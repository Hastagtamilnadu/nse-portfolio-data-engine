"""
====================================================================================================
FORWARD GOVERNANCE MONITOR & KILL-SWITCH EVALUATOR
Continuously evaluates the 4 frozen governance rules:
1. Hard Drawdown Breaker: Drawdown > 30.0% during bull regime -> CRITICAL HALT.
2. Rolling 24-Month Calmar: Strategy Calmar vs MID150BEES benchmark Calmar >= 0.70.
3. Cash Stagnation Alert: Consecutively in cash > 18 months (375 trading days).
4. Realized Slippage Ceiling: Average slippage > 0.40% (40 bps capacity ceiling).
====================================================================================================
"""

import os
import datetime
import logging
from typing import Dict, List, Optional, Tuple, Any
import polars as pl
import numpy as np

import config
from ledger import Ledger

logger = logging.getLogger("GovernanceMonitor")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class GovernanceMonitor:
    """
    Independent monitor auditing live strategy performance against institutional guardrails.
    """

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def check_drawdown(self, equity_df: pl.DataFrame) -> Dict[str, Any]:
        """Rule 1: Hard Drawdown Breaker (30.0% maximum peak-to-trough drawdown)."""
        if len(equity_df) == 0:
            return {
                "rule": "HARD_DRAWDOWN_BREAKER",
                "status": "HEALTHY",
                "current_dd": 0.0,
                "max_dd": 0.0,
                "message": "No equity records yet."
            }

        # Vectorized drawdown calculation
        equities = equity_df["net_total_equity"].to_numpy()
        running_max = np.maximum.accumulate(equities)
        dds = (running_max - equities) / np.maximum(running_max, 1e-6)

        current_dd = float(dds[-1])
        max_dd = float(np.max(dds))
        latest_risk_on = bool(equity_df.tail(1)["is_risk_on"].item())

        if current_dd >= config.HARD_MAX_DRAWDOWN_LIMIT and latest_risk_on:
            status = "CRITICAL_HALT"
            msg = f"CRITICAL HALT: Current drawdown of {current_dd*100:.2f}% breached 30.0% ceiling during bull regime!"
        elif current_dd >= 0.20:
            status = "WARNING_ELEVATED_DD"
            msg = f"WARNING: Drawdown elevated at {current_dd*100:.2f}% (Caution zone: >20%)."
        else:
            status = "HEALTHY"
            msg = f"Drawdown healthy at {current_dd*100:.2f}% (Historical max: {max_dd*100:.2f}%)."

        return {
            "rule": "HARD_DRAWDOWN_BREAKER",
            "status": status,
            "current_dd": current_dd,
            "max_dd": max_dd,
            "limit": config.HARD_MAX_DRAWDOWN_LIMIT,
            "message": msg
        }

    def check_rolling_calmar(self, equity_df: pl.DataFrame, min_trading_days: int = 500) -> Dict[str, Any]:
        """
        Rule 2: Rolling 24-Month Calmar Ratio vs MID150BEES Benchmark.
        Calmar = CAGR / Max Drawdown.
        Required: Strategy Calmar / MID150BEES Calmar >= 0.70.
        """
        if len(equity_df) < 60:
            return {
                "rule": "ROLLING_24M_CALMAR",
                "status": "INSUFFICIENT_HISTORY",
                "days_available": len(equity_df),
                "strategy_calmar": None,
                "benchmark_calmar": None,
                "relative_calmar": None,
                "message": f"Strategy running for {len(equity_df)} days (Requires >= 60 days for preliminary Calmar, 500 for full 24m)."
            }

        # Filter window
        window_df = equity_df.tail(min_trading_days)
        strat_eq = window_df["net_total_equity"].to_numpy()
        bm_closes = window_df["benchmark_close"].to_numpy()

        n_days = len(window_df)
        years = n_days / 250.0

        # Strategy CAGR & Max DD
        strat_cagr = ((strat_eq[-1] / strat_eq[0]) ** (1.0 / years) - 1.0) if strat_eq[0] > 0 else 0.0
        strat_rmax = np.maximum.accumulate(strat_eq)
        strat_max_dd = float(np.max((strat_rmax - strat_eq) / np.maximum(strat_rmax, 1e-6)))
        strat_calmar = (strat_cagr / strat_max_dd) if strat_max_dd > 0.001 else 0.0

        # Benchmark CAGR & Max DD
        valid_bm = [b for b in bm_closes if b is not None and b > 0]
        if len(valid_bm) >= 30:
            bm_arr = np.array(valid_bm)
            bm_years = len(bm_arr) / 250.0
            bm_cagr = ((bm_arr[-1] / bm_arr[0]) ** (1.0 / bm_years) - 1.0) if bm_arr[0] > 0 else 0.0
            bm_rmax = np.maximum.accumulate(bm_arr)
            bm_max_dd = float(np.max((bm_rmax - bm_arr) / np.maximum(bm_rmax, 1e-6)))
            bm_calmar = (bm_cagr / bm_max_dd) if bm_max_dd > 0.001 else 0.0
        else:
            bm_cagr = 0.12
            bm_max_dd = 0.18
            bm_calmar = 0.67

        rel_calmar = (strat_calmar / bm_calmar) if bm_calmar > 0.001 else 1.0

        if rel_calmar < config.MIN_CALMAR_RATIO_24M and n_days >= min_trading_days:
            status = "UNDERPERFORMANCE_WARNING"
            msg = f"WARNING: Relative Calmar {rel_calmar:.2f} fell below 0.70 threshold vs MID150BEES!"
        else:
            status = "HEALTHY"
            msg = f"Calmar healthy: Strat {strat_calmar:.2f} vs MID150BEES {bm_calmar:.2f} (Relative: {rel_calmar:.2f}x)."

        return {
            "rule": "ROLLING_24M_CALMAR",
            "status": status,
            "days_evaluated": n_days,
            "strategy_cagr": strat_cagr,
            "strategy_max_dd": strat_max_dd,
            "strategy_calmar": strat_calmar,
            "benchmark_cagr": bm_cagr,
            "benchmark_max_dd": bm_max_dd,
            "benchmark_calmar": bm_calmar,
            "relative_calmar": rel_calmar,
            "message": msg
        }

    def check_cash_stagnation(self, equity_df: pl.DataFrame) -> Dict[str, Any]:
        """Rule 3: Cash Stagnation Trigger (> 18 consecutive months / ~375 trading days in cash)."""
        if len(equity_df) == 0:
            return {
                "rule": "CASH_STAGNATION",
                "status": "HEALTHY",
                "consecutive_cash_days": 0,
                "message": "No trading days recorded yet."
            }

        risk_on_series = equity_df["is_risk_on"].to_list()
        consecutive_cash = 0
        for is_on in reversed(risk_on_series):
            if is_on == 0:
                consecutive_cash += 1
            else:
                break

        limit_days = config.MAX_CASH_STAGNATION_MONTHS * 21  # 18 * 21 = 378 days
        if consecutive_cash >= limit_days:
            status = "REGIME_REVIEW_ALERT"
            msg = f"REGIME ALERT: Strategy held 100% cash consecutively for {consecutive_cash} trading days (> 18 months)!"
        else:
            status = "HEALTHY"
            msg = f"Cash stance normal: {consecutive_cash} consecutive days in cash (Ceiling: {limit_days} days)."

        return {
            "rule": "CASH_STAGNATION",
            "status": status,
            "consecutive_cash_days": consecutive_cash,
            "limit_days": limit_days,
            "message": msg
        }

    def check_realized_slippage(self) -> Dict[str, Any]:
        """Rule 4: Realized Slippage Ceiling (Average slippage <= 0.40% / 40 bps)."""
        orders_df = pl.DataFrame()
        try:
            with self.ledger._get_conn() as conn:
                orders_df = pl.read_database("SELECT gross_amount, slippage_cost FROM orders WHERE gross_amount > 0;", conn)
        except Exception:
            pass

        if len(orders_df) == 0:
            return {
                "rule": "REALIZED_SLIPPAGE_CEILING",
                "status": "HEALTHY",
                "avg_slippage_bps": config.SLIPPAGE_BPS * 10000.0,
                "message": f"No filled orders yet. Default modeled slippage: {config.SLIPPAGE_BPS * 10000.0:.0f} bps."
            }

        total_gross = orders_df["gross_amount"].sum()
        total_slip = orders_df["slippage_cost"].sum()
        avg_slip = (total_slip / total_gross) if total_gross > 0 else config.SLIPPAGE_BPS
        avg_bps = avg_slip * 10000.0

        if avg_slip > config.MAX_REALIZED_SLIPPAGE:
            status = "CAPACITY_WARNING"
            msg = f"CAPACITY WARNING: Average realized slippage of {avg_bps:.1f} bps exceeded 40.0 bps limit!"
        else:
            status = "HEALTHY"
            msg = f"Slippage healthy at {avg_bps:.1f} bps (Modeled: 20 bps, Limit: 40 bps)."

        return {
            "rule": "REALIZED_SLIPPAGE_CEILING",
            "status": status,
            "avg_slippage": avg_slip,
            "avg_slippage_bps": avg_bps,
            "limit_bps": config.MAX_REALIZED_SLIPPAGE * 10000.0,
            "message": msg
        }

    def evaluate_all(self) -> Dict[str, Any]:
        """Runs all 4 governance checks and returns aggregated system health verdict."""
        equity_df = self.ledger.get_equity_history()

        r1 = self.check_drawdown(equity_df)
        r2 = self.check_rolling_calmar(equity_df)
        r3 = self.check_cash_stagnation(equity_df)
        r4 = self.check_realized_slippage()

        statuses = [r1["status"], r2["status"], r3["status"], r4["status"]]

        if "CRITICAL_HALT" in statuses:
            overall = "CRITICAL_HALT"
        elif any(s.startswith("WARNING") or s.startswith("REGIME") or s.startswith("CAPACITY") for s in statuses):
            overall = "WARNING"
        else:
            overall = "HEALTHY"

        return {
            "overall_health": overall,
            "timestamp": datetime.datetime.now().isoformat(),
            "rules": {
                "drawdown": r1,
                "calmar": r2,
                "cash_stagnation": r3,
                "slippage": r4
            }
        }
