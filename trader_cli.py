"""
====================================================================================================
AUTONOMOUS MOMENTUM PAPER TRADER: USER CONTROL CLI
Command-line interface for the 23-year audited 12-stock NSE momentum trading system.
Provides: status, run, mode, add-ca, init, and reconcile.
====================================================================================================
"""

import os
import sys
import argparse
import datetime
import logging
from typing import Dict, List, Optional, Any
import polars as pl

import config
import canonical_momentum_core as core
from trading_calendar import TradingCalendar
from data_feed import DataFeed
from corporate_action_watcher import CorporateActionWatcher
from ledger import Ledger
from execution_router import ExecutionRouter
from governance_monitor import GovernanceMonitor

# Configure root logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("TraderCLI")


def cmd_init(args):
    """Initializes or resets the paper trading portfolio."""
    capital = args.capital or config.DEFAULT_INITIAL_CAPITAL
    inception = args.date or TradingCalendar.format_date(datetime.datetime.now().date())
    
    # Ensure inception date is a trading day
    incept_dt = TradingCalendar.parse_date(inception)
    if not TradingCalendar.is_trading_day(incept_dt):
        inception = TradingCalendar.format_date(TradingCalendar.next_trading_day(incept_dt))

    ledger = Ledger()
    ledger.initialize_portfolio(initial_capital=capital, inception_date=inception, reset=args.reset)
    
    schedule = TradingCalendar.get_anchored_rebalance_schedule(inception, horizon_days=500, cadence=config.REBALANCE_CADENCE)
    first_rebal = schedule[0]
    next_rebal = schedule[1] if len(schedule) > 1 else schedule[0]
    ledger.update_rebalance_dates(last_rebalance_date=None, next_rebalance_date=first_rebal, rebalance_index=0)
    
    print("\n" + "=" * 80)
    print(f"  PORTFOLIO INITIALIZED SUCCESSFULLY")
    print(f"  Initial Capital: Rs {capital:,.2f}")
    print(f"  Inception Date:  {inception}")
    print(f"  First Rebalance: {first_rebal}")
    print(f"  Next Rebalance:  {next_rebal}")
    print("=" * 80 + "\n")


def cmd_status(args):
    """Displays comprehensive portfolio health, holdings, and governance audit."""
    ledger = Ledger()
    state = ledger.get_state()
    positions = ledger.get_positions()
    gov = GovernanceMonitor(ledger)
    health = gov.evaluate_all()

    feed = DataFeed()
    latest_date = state.get("last_rebalance_date") or state["inception_date"]

    total_net = state["net_equity"]
    initial_cap = state["peak_equity"]  # baseline or initial
    cash = state["current_cash"]
    cash_pct = (cash / total_net * 100.0) if total_net > 0 else 100.0
    
    # Calculate holdings value
    holdings_val = sum(p["current_value"] for p in positions.values())
    dd_data = health["rules"]["drawdown"]
    current_dd = dd_data["current_dd"] * 100.0

    print("\n" + "=" * 90)
    print("               AUTONOMOUS MOMENTUM PAPER TRADER: STATUS AUDIT")
    print(f"Mode: {config.ACTIVE_TRADING_MODE.value} | Cost Model: {config.ACTIVE_COST_MODEL.value} | As Of: {state.get('updated_at', '')[:10]}")
    print("=" * 90)
    
    print("\nPORTFOLIO CAPITAL & LIQUIDITY:")
    print(f"  Total Portfolio Net Equity: Rs {total_net:>14,.2f}")
    print(f"  Current Idle Cash Reserve:  Rs {cash:>14,.2f} ({cash_pct:.1f}% allocation)")
    print(f"  Active Stock Holdings:      Rs {holdings_val:>14,.2f} ({len(positions)} positions)")
    print(f"  High-Water Mark (Peak):     Rs {state['peak_equity']:>14,.2f}")
    print(f"  Current Peak Drawdown:      {current_dd:>14.2f}% (Circuit Breaker: {config.HARD_MAX_DRAWDOWN_LIMIT*100:.1f}%)")

    print("\nREBALANCE CADENCE & REGIME:")
    print(f"  Inception Date:             {state['inception_date']}")
    print(f"  Last Rebalance Executed:    {state.get('last_rebalance_date', 'None')}")
    print(f"  Next Scheduled Rebalance:   {state.get('next_rebalance_date', 'Pending')}")
    print(f"  Cadence Anchor:             Every {config.REBALANCE_CADENCE} Trading Days")
    print(f"  Market Breadth Gate:        {'RISK-ON (BULL)' if state.get('is_risk_on', 1) else 'RISK-OFF (CASH DEFENSE)'}")

    print("\nACTIVE PORTFOLIO HOLDINGS:")
    if not positions:
        print("  (No active equity holdings - portfolio is currently 100% in cash/liquid reserve)")
    else:
        print(f"  {'SYMBOL':<14} | {'SHARES':>7} | {'ENTRY PX':>12} | {'LAST PX':>12} | {'P&L (%)':>10} | {'CURRENT VALUE':>15}")
        print("  " + "-" * 84)
        for sym, pos in sorted(positions.items()):
            pnl_pct = ((pos["last_price"] / pos["entry_price"]) - 1.0) * 100.0 if pos["entry_price"] > 0 else 0.0
            pnl_str = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%"
            val = pos["current_value"]
            print(f"  {sym:<14} | {pos['shares']:>7} | Rs {pos['entry_price']:>9.2f} | Rs {pos['last_price']:>9.2f} | {pnl_str:>10} | Rs {val:>12,.2f}")

    print("\nFORWARD GOVERNANCE HEALTH AUDIT:")
    for rule_key, rule_res in health["rules"].items():
        status_tag = f"[{rule_res['status']}]"
        print(f"  {status_tag:<26} {rule_res['message']}")
    
    overall_color = health["overall_health"]
    print(f"\n  OVERALL HEALTH VERDICT: [{overall_color}]")
    print("=" * 90 + "\n")


def cmd_run(args):
    """Executes the daily automated workflow."""
    ledger = Ledger()
    state = ledger.get_state()
    calendar = TradingCalendar()
    feed = DataFeed()
    ca_watcher = CorporateActionWatcher()
    router = ExecutionRouter(ledger, mode=config.ACTIVE_TRADING_MODE)

    # Determine execution dates to process (with automatic catch-up of missed trading days)
    if args.date:
        dates_to_process = [args.date]
    else:
        today_dt = datetime.datetime.now().date()
        target_dt = today_dt if calendar.is_trading_day(today_dt) else calendar.prev_trading_day(today_dt)
        
        # Check last marked date in daily_equity to catch up any missed days (e.g. post-holiday delays)
        with ledger._get_conn() as conn:
            cur = conn.execute("SELECT date FROM daily_equity ORDER BY date DESC LIMIT 1").fetchone()
            last_date_str = cur[0] if cur else None
            
        if last_date_str:
            last_dt = calendar.parse_date(last_date_str)
            dates_to_process = []
            curr = calendar.next_trading_day(last_dt)
            while curr <= target_dt:
                dates_to_process.append(calendar.format_date(curr))
                curr = calendar.next_trading_day(curr)
            if not dates_to_process:
                logger.info(f"Portfolio is already up-to-date through {last_date_str}. No pending trading days to process.")
                return
        else:
            dates_to_process = [calendar.format_date(target_dt)]

    for exec_date in dates_to_process:
        state = ledger.get_state()
        logger.info(f"Running automated cycle for trading day: {exec_date}")

        # Step 1: Ingest Bhavcopy for exec_date
        bhav_df = feed.get_daily_bhavcopy(exec_date)
        if bhav_df is None or len(bhav_df) == 0:
            logger.warning(f"Bhavcopy not available for {exec_date}. Exiting cycle.")
            return

        price_lookup = feed.get_price_lookup(exec_date)
        closing_lookup = feed.get_closing_lookup(exec_date)
        bm_close = feed.get_benchmark_close(exec_date)

        # Step 2: Corporate Action Audit on active holdings
        positions = ledger.get_positions()
        if positions:
            ca_adjustments = ca_watcher.audit_and_adjust_holdings(positions, exec_date, price_lookup)
            for adj in ca_adjustments:
                ledger.log_corporate_action(
                    date_str=exec_date,
                    symbol=adj["Symbol"],
                    action_type=adj["Action_Type"],
                    factor=adj["Factor"],
                    source=adj["Source"],
                    old_shares=adj["Old_Shares"],
                    new_shares=adj["New_Shares"],
                    old_price=adj["Old_Entry_Price"],
                    new_price=adj["New_Entry_Price"]
                )

        # Step 3: Check Rebalance Cadence
        schedule = calendar.get_anchored_rebalance_schedule(state["inception_date"], horizon_days=1000, cadence=config.REBALANCE_CADENCE)
        next_rebal = state.get("next_rebalance_date")

        # If next_rebal is not set, initialize to schedule[0]
        if not next_rebal:
            next_rebal = schedule[0]

        is_rebalance_day = (exec_date == next_rebal) or (exec_date in schedule and exec_date != state.get("last_rebalance_date"))
        is_risk_on = True

        if is_rebalance_day:
            logger.info(f"==> TODAY ({exec_date}) IS AN ANCHORED REBALANCE EXECUTION DATE! <==")
            
            # Calculate signal date (T-2 trading day)
            t_exec_dt = calendar.parse_date(exec_date)
            t_minus_1 = calendar.prev_trading_day(t_exec_dt)
            t_minus_2 = calendar.prev_trading_day(t_minus_1)
            signal_date = calendar.format_date(t_minus_2)
            logger.info(f"Rebalance Sequence: Signal Date={signal_date} (T-2) -> Execution Date={exec_date} (T Open)")

            # Load market slice up to signal date
            slice_df = feed.get_recent_market_slice(eval_date=signal_date, lookback_calendar_days=500)
            
            # Compute market breadth
            breadth_df = core.compute_market_breadth(slice_df)
            sub_b = breadth_df.filter(pl.col("Date") == signal_date)
            if len(sub_b) > 0:
                b_ma5 = float(sub_b["breadth_ma5"].item())
                is_risk_on = core.should_be_risk_on(b_ma5)
                logger.info(f"Market Breadth on {signal_date}: {b_ma5*100:.2f}% (Risk-On Bull: {is_risk_on})")
            else:
                is_risk_on = True
                logger.warning(f"Breadth data missing for {signal_date}. Defaulting to RISK-ON.")

            # Build qualified universe & select candidates
            qualified_df = core.build_qualified_universe(slice_df, require_next_open=False)
            top_candidates = core.select_top_momentum(qualified_df, signal_date, n=config.NUM_STOCKS)
            backup_candidates = core.select_top_momentum(qualified_df, signal_date, n=config.NUM_STOCKS + 5)[config.NUM_STOCKS:]

            logger.info(f"Top {config.NUM_STOCKS} Momentum Candidates: {top_candidates}")
            logger.info(f"Backup Candidates (#13-#17): {backup_candidates}")

            # Assemble market data on exec_date for order generation
            exec_bhav = feed.get_daily_bhavcopy(exec_date)
            mdata = {}
            if exec_bhav is not None:
                for r in exec_bhav.select(["Symbol", "PrevClose", "Open", "High", "Low", "Close"]).to_dicts():
                    mdata[r["Symbol"]] = {
                        "PrevClose": float(r["PrevClose"] or 0),
                        "Open": float(r["Open"] or 0),
                        "High": float(r["High"] or 0),
                        "Low": float(r["Low"] or 0),
                        "Close": float(r["Close"] or 0),
                    }

            # Generate orders and execute/stage
            plan = router.generate_orders(
                signal_date=signal_date,
                exec_date=exec_date,
                target_symbols=top_candidates,
                backup_candidates=backup_candidates,
                is_risk_on=is_risk_on,
                market_data=mdata
            )

            res = router.execute_plan(plan, cost_model=config.ACTIVE_COST_MODEL)
            logger.info(f"Execution Result: {res}")

            # Advance schedule to next rebalance date
            cur_idx = schedule.index(exec_date) if exec_date in schedule else state.get("rebalance_index", 0)
            next_idx = cur_idx + 1
            subsequent_rebal = schedule[next_idx] if next_idx < len(schedule) else schedule[-1]
            ledger.update_rebalance_dates(last_rebalance_date=exec_date, next_rebalance_date=subsequent_rebal, rebalance_index=next_idx)
        else:
            logger.info(f"Non-rebalance trading day ({exec_date}). Proceeding with mark-to-market and cash yield accrual.")

        # Step 4: Mark to Market & Accrue Daily Cash Yield
        snap = ledger.mark_to_market(
            date_str=exec_date,
            closing_prices=closing_lookup,
            benchmark_close=bm_close,
            is_risk_on=is_risk_on
        )
        logger.info(f"Daily Mark: Total Equity Rs {snap['total_equity']:,.2f} (Cash: Rs {snap['cash']:,.2f}, Yield: Rs {snap['yield_accrued']:.2f})")

        # Step 5: Export CSV reports
        ledger.export_reports()

        # Step 6: Governance Check
        gov = GovernanceMonitor(ledger)
        health = gov.evaluate_all()
        logger.info(f"Governance Health Verdict: [{health['overall_health']}]")


def cmd_mode(args):
    """Toggles active trading mode between PAPER and LIVE_SIGNALS."""
    new_mode = args.target_mode.upper()
    if new_mode not in ["PAPER", "LIVE_SIGNALS", "SIGNALS"]:
        print("Invalid mode. Choose 'PAPER' or 'LIVE_SIGNALS'.")
        return

    mode_val = "TradingMode.PAPER" if new_mode == "PAPER" else "TradingMode.LIVE_SIGNALS"
    config_path = os.path.join(config.BASE_DIR, "config.py")

    # Update config.py ACTIVE_TRADING_MODE
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    new_lines = []
    for line in lines:
        if line.startswith("ACTIVE_TRADING_MODE:"):
            new_lines.append(f"ACTIVE_TRADING_MODE: TradingMode = {mode_val}")
        else:
            new_lines.append(line)

    with open(config_path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")

    print(f"\nTrading mode switched to: {new_mode}\n")


def cmd_add_ca(args):
    """Appends a confirmed corporate action to reference CSV."""
    watcher = CorporateActionWatcher()
    watcher.add_reference_action(
        symbol=args.symbol,
        ex_date=args.ex_date,
        action_type=args.action,
        factor=float(args.factor),
        details=args.details or ""
    )
    print(f"\nSuccessfully added corporate action for {args.symbol.upper()} on {args.ex_date} (Factor: {args.factor})\n")


def cmd_reconcile(args):
    """Executes the audited parity verification against historical benchmark."""
    import subprocess
    verify_script = os.path.join(config.BASE_DIR, "verify_parity.py")
    subprocess.run([sys.executable, verify_script], check=True)


def cmd_calendar(args):
    """Displays official upcoming NSE trading holidays and anchored rebalance milestones."""
    ledger = Ledger()
    state = ledger.get_state()
    incept = state.get("inception_date")
    if not incept:
        print("\nPortfolio not yet initialized. Run 'python trader_cli.py init' first.\n")
        return
    next_reb = state.get("next_rebalance_date")
    
    print("\n" + "=" * 80)
    print("               NSE TRADING CALENDAR & REBALANCE SCHEDULE AUDIT")
    print("=" * 80)
    
    today_str = TradingCalendar.format_date(datetime.datetime.now().date())
    upcoming_holidays = TradingCalendar.get_upcoming_holidays(today_str, days_ahead=60)
    print("\nUPCOMING NSE TRADING HOLIDAYS (Next 60 Days):")
    if upcoming_holidays:
        for d, day_name, occ in upcoming_holidays:
            print(f"  * {d} ({day_name:<9}): {occ}")
    else:
        print("  (No exchange holidays in the next 60 days)")

    schedule = TradingCalendar.get_anchored_rebalance_schedule(incept, horizon_days=500, cadence=config.REBALANCE_CADENCE)
    print("\nANCHORED 21-TRADING-DAY REBALANCE MILESTONES:")
    for idx, reb_date in enumerate(schedule[:6]):
        tag = ""
        if reb_date == next_reb:
            tag = "  <-- NEXT SCHEDULED REBALANCE"
        elif idx == 0:
            tag = "  (Inception Rebalance Executed)"
        print(f"  Rebalance #{idx:<2}: {reb_date}{tag}")
    print("=" * 80 + "\n")


def cmd_add_holiday(args):
    """Adds a new holiday to reference_nse_holidays.csv and updates trading calendar."""
    TradingCalendar.add_holiday(args.date, args.occasion)
    print(f"\nSuccessfully registered NSE holiday on {args.date}: '{args.occasion}'\n")


def main():
    parser = argparse.ArgumentParser(description="Autonomous Momentum Paper Trader CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # init
    p_init = subparsers.add_parser("init", help="Initialize paper portfolio")
    p_init.add_argument("--capital", type=float, default=config.DEFAULT_INITIAL_CAPITAL, help="Initial capital in INR")
    p_init.add_argument("--date", type=str, default=None, help="Inception date (YYYY-MM-DD)")
    p_init.add_argument("--reset", action="store_true", help="Force reset existing state")

    # status
    subparsers.add_parser("status", help="View portfolio holdings and governance status")

    # calendar
    subparsers.add_parser("calendar", help="View upcoming holidays and rebalance schedule milestones")

    # add-holiday
    p_h = subparsers.add_parser("add-holiday", help="Register a newly announced NSE holiday")
    p_h.add_argument("date", help="Holiday Date (YYYY-MM-DD)")
    p_h.add_argument("occasion", help="Occasion or Reason (e.g. 'Ganesh Chaturthi')")

    # run
    p_run = subparsers.add_parser("run", help="Run automated daily cycle")
    p_run.add_argument("--date", type=str, default=None, help="Execute for specific date (YYYY-MM-DD)")

    # mode
    p_mode = subparsers.add_parser("mode", help="Switch mode between PAPER and LIVE_SIGNALS")
    p_mode.add_argument("target_mode", choices=["paper", "signals", "PAPER", "LIVE_SIGNALS"], help="Target mode")

    # add-ca
    p_ca = subparsers.add_parser("add-ca", help="Add confirmed corporate action to reference table")
    p_ca.add_argument("symbol", help="Stock Symbol (e.g. RELIANCE)")
    p_ca.add_argument("ex_date", help="Ex-Date (YYYY-MM-DD)")
    p_ca.add_argument("action", help="Action Type (SPLIT / BONUS / DEMERGER)")
    p_ca.add_argument("factor", type=float, help="Adjustment factor (e.g. 2.0, 5.0, 1.0)")
    p_ca.add_argument("--details", default="", help="Description details")

    # reconcile
    subparsers.add_parser("reconcile", help="Verify 100% bit-for-bit parity with backtest")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    cmd_map = {
        "init": cmd_init,
        "status": cmd_status,
        "calendar": cmd_calendar,
        "add-holiday": cmd_add_holiday,
        "run": cmd_run,
        "mode": cmd_mode,
        "add-ca": cmd_add_ca,
        "reconcile": cmd_reconcile
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
