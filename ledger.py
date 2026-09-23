"""
====================================================================================================
SQLITE LEDGER & TRANSACTION ACCOUNTING
ACID-compliant relational ledger for portfolio state, positions, trade orders,
daily mark-to-market equity curves, and corporate action audit trail.
Tracks both gross equity (bit-for-bit backtest parity) and net equity (production realistic).
====================================================================================================
"""

import os
import sqlite3
import datetime
import logging
from typing import Dict, List, Optional, Tuple, Any
import polars as pl

import config
import canonical_momentum_core as core

logger = logging.getLogger("Ledger")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class Ledger:
    """
    ACID transactional ledger backing the Autonomous Momentum Paper Trader.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_db(self) -> None:
        """Initializes database schema if not already created."""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS portfolio_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    current_cash REAL NOT NULL,
                    peak_equity REAL NOT NULL,
                    inception_date TEXT NOT NULL,
                    last_rebalance_date TEXT,
                    next_rebalance_date TEXT,
                    rebalance_index INTEGER DEFAULT 0,
                    is_risk_on INTEGER DEFAULT 1,
                    gross_equity REAL NOT NULL,
                    net_equity REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    shares INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    entry_date TEXT NOT NULL,
                    last_price REAL NOT NULL,
                    current_value REAL NOT NULL,
                    pending_exit INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    shares INTEGER NOT NULL,
                    price REAL NOT NULL,
                    gross_amount REAL NOT NULL,
                    slippage_cost REAL NOT NULL,
                    dp_charge REAL NOT NULL,
                    net_amount REAL NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_equity (
                    date TEXT PRIMARY KEY,
                    cash REAL NOT NULL,
                    positions_value REAL NOT NULL,
                    gross_total_equity REAL NOT NULL,
                    net_total_equity REAL NOT NULL,
                    benchmark_close REAL,
                    daily_cash_yield REAL DEFAULT 0.0,
                    num_positions INTEGER DEFAULT 0,
                    is_risk_on INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS corporate_action_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    factor REAL NOT NULL,
                    source TEXT NOT NULL,
                    old_shares INTEGER NOT NULL,
                    new_shares INTEGER NOT NULL,
                    old_entry_price REAL NOT NULL,
                    new_entry_price REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    def initialize_portfolio(
        self,
        initial_capital: float = config.DEFAULT_INITIAL_CAPITAL,
        inception_date: str = "2026-01-01",
        reset: bool = False
    ) -> None:
        """Initializes or resets portfolio state."""
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM portfolio_state WHERE id = 1;")
            exists = cur.fetchone()[0] > 0

            if exists and not reset:
                logger.info("Portfolio already initialized. Retaining active state.")
                return

            now_str = datetime.datetime.now().isoformat()
            if reset:
                conn.executescript("""
                    DELETE FROM portfolio_state;
                    DELETE FROM positions;
                    DELETE FROM orders;
                    DELETE FROM daily_equity;
                    DELETE FROM corporate_action_logs;
                """)

            cur.execute("""
                INSERT INTO portfolio_state (
                    id, current_cash, peak_equity, inception_date,
                    last_rebalance_date, next_rebalance_date, rebalance_index,
                    is_risk_on, gross_equity, net_equity, updated_at
                ) VALUES (1, ?, ?, ?, NULL, NULL, 0, 1, ?, ?, ?);
            """, (initial_capital, initial_capital, inception_date, initial_capital, initial_capital, now_str))
            logger.info(f"Initialized portfolio with Rs {initial_capital:,.2f} on {inception_date}")

    def get_state(self) -> Dict[str, Any]:
        """Fetches current portfolio state singleton."""
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM portfolio_state WHERE id = 1;")
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("Portfolio not initialized. Run initialize_portfolio() first.")
            return dict(row)

    def get_positions(self) -> Dict[str, Dict[str, Any]]:
        """Returns currently held positions indexed by Symbol."""
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM positions;")
            rows = cur.fetchall()
            return {r["symbol"]: dict(r) for r in rows}

    def execute_buy(
        self,
        date_str: str,
        symbol: str,
        shares: int,
        price: float,
        reason: str = "REBALANCE_ENTRY",
        cost_model: config.CostModel = config.ACTIVE_COST_MODEL
    ) -> float:
        """
        Executes BUY order with slippage and atomic position recording.
        Returns total cash deducted.
        """
        if shares <= 0 or price <= 0:
            return 0.0

        gross = shares * price
        slippage = gross * config.SLIPPAGE_BPS
        dp_charge = 0.0  # DP fee applies only to delivery sell
        total_cost = gross + slippage + dp_charge

        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            cur = conn.cursor()
            # Deduct cash
            cur.execute("UPDATE portfolio_state SET current_cash = current_cash - ?, updated_at = ? WHERE id = 1;", (total_cost, now_str))

            # Upsert position
            cur.execute("""
                INSERT INTO positions (symbol, shares, entry_price, entry_date, last_price, current_value, pending_exit, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    shares = shares + excluded.shares,
                    entry_price = excluded.entry_price,
                    last_price = excluded.last_price,
                    current_value = (shares + excluded.shares) * excluded.last_price,
                    updated_at = excluded.updated_at;
            """, (symbol, shares, price, date_str, price, gross, now_str, now_str))

            # Record order
            cur.execute("""
                INSERT INTO orders (date, symbol, side, shares, price, gross_amount, slippage_cost, dp_charge, net_amount, reason, created_at)
                VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?);
            """, (date_str, symbol, shares, price, gross, slippage, dp_charge, total_cost, reason, now_str))

        logger.info(f"[ORDER BUY] {symbol}: {shares} shares @ Rs {price:.2f} | Gross: Rs {gross:,.2f} | Slippage: Rs {slippage:.2f}")
        return total_cost

    def execute_sell(
        self,
        date_str: str,
        symbol: str,
        shares: int,
        price: float,
        reason: str = "REBALANCE_EXIT",
        cost_model: config.CostModel = config.ACTIVE_COST_MODEL
    ) -> float:
        """
        Executes SELL order with slippage, DP charges, and position removal.
        Returns net cash credited.
        """
        if shares <= 0 or price <= 0:
            return 0.0

        gross = shares * price
        slippage = gross * config.SLIPPAGE_BPS
        dp_charge = config.DP_CHARGE_PER_EXIT if cost_model == config.CostModel.PRODUCTION_REALISTIC else 0.0
        net_proceeds = gross - slippage - dp_charge

        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            cur = conn.cursor()
            # Credit cash
            cur.execute("UPDATE portfolio_state SET current_cash = current_cash + ?, updated_at = ? WHERE id = 1;", (net_proceeds, now_str))

            # Remove or decrement position
            cur.execute("SELECT shares FROM positions WHERE symbol = ?;", (symbol,))
            row = cur.fetchone()
            if row:
                cur_shares = row["shares"]
                if cur_shares <= shares:
                    cur.execute("DELETE FROM positions WHERE symbol = ?;", (symbol,))
                else:
                    cur.execute("""
                        UPDATE positions SET
                            shares = shares - ?,
                            current_value = (shares - ?) * ?,
                            updated_at = ?
                        WHERE symbol = ?;
                    """, (shares, shares, price, now_str, symbol))

            # Record order
            cur.execute("""
                INSERT INTO orders (date, symbol, side, shares, price, gross_amount, slippage_cost, dp_charge, net_amount, reason, created_at)
                VALUES (?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?);
            """, (date_str, symbol, shares, price, gross, slippage, dp_charge, net_proceeds, reason, now_str))

        logger.info(
            f"[ORDER SELL] {symbol}: {shares} shares @ Rs {price:.2f} | "
            f"Gross: Rs {gross:,.2f} | Slippage: Rs {slippage:.2f} | DP: Rs {dp_charge:.2f} | Net: Rs {net_proceeds:,.2f}"
        )
        return net_proceeds

    def mark_pending_exit(self, symbol: str, pending: bool = True) -> None:
        """Flags position as locked in lower circuit awaiting exit at next open."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE positions SET pending_exit = ?, updated_at = ? WHERE symbol = ?;",
                (1 if pending else 0, datetime.datetime.now().isoformat(), symbol)
            )

    def log_corporate_action(
        self,
        date_str: str,
        symbol: str,
        action_type: str,
        factor: float,
        source: str,
        old_shares: int,
        new_shares: int,
        old_price: float,
        new_price: float
    ) -> None:
        """Persists corporate action adjustment event and updates position in-place."""
        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO corporate_action_logs (
                    date, symbol, action_type, factor, source,
                    old_shares, new_shares, old_entry_price, new_entry_price, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (date_str, symbol, action_type, factor, source, old_shares, new_shares, old_price, new_price, now_str))

            cur.execute("""
                UPDATE positions SET
                    shares = ?,
                    entry_price = ?,
                    current_value = ? * last_price,
                    updated_at = ?
                WHERE symbol = ?;
            """, (new_shares, new_price, new_shares, now_str, symbol))

    def mark_to_market(
        self,
        date_str: str,
        closing_prices: Dict[str, float],
        benchmark_close: Optional[float] = None,
        is_risk_on: bool = True
    ) -> Dict[str, Any]:
        """
        Marks all held positions to daily close, accrues cash yield, and records daily equity snapshot.
        Dual tracking:
        - gross_total_equity: bit-for-bit backtest parity comparator
        - net_total_equity: real post-fee capital
        """
        positions = self.get_positions()
        state = self.get_state()

        # Step 1: Accrue daily risk-free interest on idle cash (idempotent if re-marking same date)
        with self._get_conn() as conn:
            existing_row = conn.execute("SELECT cash, daily_cash_yield FROM daily_equity WHERE date = ?", (date_str,)).fetchone()

        current_cash = state["current_cash"]
        if existing_row:
            daily_yield = existing_row["daily_cash_yield"]
            new_cash = current_cash
        else:
            daily_yield = core.accrue_daily_cash_yield(current_cash)
            new_cash = current_cash + daily_yield

        # Step 2: Mark positions to close
        positions_value = 0.0
        now_str = datetime.datetime.now().isoformat()
        with self._get_conn() as conn:
            cur = conn.cursor()
            for sym, pos in positions.items():
                last_px = closing_prices.get(sym, pos["last_price"])
                c_val = pos["shares"] * last_px
                positions_value += c_val
                cur.execute("""
                    UPDATE positions SET
                        last_price = ?,
                        current_value = ?,
                        updated_at = ?
                    WHERE symbol = ?;
                """, (last_px, c_val, now_str, sym))

            # Total Equity
            total_equity = new_cash + positions_value
            peak_equity = max(state["peak_equity"], total_equity)

            # Update state
            cur.execute("""
                UPDATE portfolio_state SET
                    current_cash = ?,
                    peak_equity = ?,
                    is_risk_on = ?,
                    gross_equity = ?,
                    net_equity = ?,
                    updated_at = ?
                WHERE id = 1;
            """, (new_cash, peak_equity, 1 if is_risk_on else 0, total_equity, total_equity, now_str))

            # Insert daily snapshot
            cur.execute("""
                INSERT INTO daily_equity (
                    date, cash, positions_value, gross_total_equity, net_total_equity,
                    benchmark_close, daily_cash_yield, num_positions, is_risk_on
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    cash = excluded.cash,
                    positions_value = excluded.positions_value,
                    gross_total_equity = excluded.gross_total_equity,
                    net_total_equity = excluded.net_total_equity,
                    benchmark_close = excluded.benchmark_close,
                    daily_cash_yield = excluded.daily_cash_yield,
                    num_positions = excluded.num_positions,
                    is_risk_on = excluded.is_risk_on;
            """, (date_str, new_cash, positions_value, total_equity, total_equity, benchmark_close, daily_yield, len(positions), 1 if is_risk_on else 0))

        return {
            "date": date_str,
            "cash": new_cash,
            "positions_value": positions_value,
            "total_equity": total_equity,
            "peak_equity": peak_equity,
            "yield_accrued": daily_yield,
            "num_positions": len(positions),
            "is_risk_on": is_risk_on
        }

    def update_rebalance_dates(
        self,
        last_rebalance_date: str,
        next_rebalance_date: str,
        rebalance_index: int
    ) -> None:
        """Updates rebalance tracking markers in portfolio state."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE portfolio_state SET
                    last_rebalance_date = ?,
                    next_rebalance_date = ?,
                    rebalance_index = ?,
                    updated_at = ?
                WHERE id = 1;
            """, (last_rebalance_date, next_rebalance_date, rebalance_index, datetime.datetime.now().isoformat()))

    def export_reports(self) -> Tuple[str, str]:
        """Exports portfolio_summary.csv and trade_history.csv to reports directory."""
        reports_dir = config.REPORTS_DIR
        os.makedirs(reports_dir, exist_ok=True)

        summary_csv = os.path.join(reports_dir, "portfolio_summary.csv")
        trades_csv = os.path.join(reports_dir, "trade_history.csv")
        ca_csv = os.path.join(reports_dir, "corporate_actions.csv")

        with self._get_conn() as conn:
            # Portfolio summary (active positions + cash)
            positions = pl.read_database("SELECT * FROM positions;", conn)
            positions.write_csv(summary_csv)

            # Trade history
            orders = pl.read_database("SELECT * FROM orders ORDER BY order_id DESC;", conn)
            orders.write_csv(trades_csv)

            # Corporate actions log
            ca_logs = pl.read_database("SELECT * FROM corporate_action_logs ORDER BY id DESC;", conn)
            ca_logs.write_csv(ca_csv)

        logger.info(f"Exported reports to {reports_dir}")
        return summary_csv, trades_csv

    def get_equity_history(self) -> pl.DataFrame:
        """Loads time-series of daily equity snapshots for governance and Calmar analysis."""
        with self._get_conn() as conn:
            df = pl.read_database("SELECT * FROM daily_equity ORDER BY date ASC;", conn)
            return df
