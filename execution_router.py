"""
====================================================================================================
EXECUTION ROUTER & MARKET MICROSTRUCTURE HANDLER
Routes orders based on active TradingMode (PAPER vs LIVE_SIGNALS).
Handles Indian market microstructure:
- Upper-circuit on buy -> fallback to Candidate #13, #14, etc.
- Lower-circuit on sell -> marks PENDING_EXIT for next-day liquidation
- Dual cost accounting (flat 20 bps vs 20 bps + ₹15.93 DP charge)
- Equal-weight integer share sizing
====================================================================================================
"""

import os
import json
import logging
from typing import Dict, List, Optional, Tuple, Any

import config
from ledger import Ledger

logger = logging.getLogger("ExecutionRouter")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class ExecutionRouter:
    """
    Manages order execution, circuit freeze fallbacks, and staging tickets.
    """

    def __init__(self, ledger: Ledger, mode: config.TradingMode = config.ACTIVE_TRADING_MODE):
        self.ledger = ledger
        self.mode = mode

    def is_upper_circuit(self, prev_close: float, open_px: float, high_px: float, low_px: float, close_px: float) -> bool:
        """Detects if a stock is locked at upper circuit (no sellers)."""
        if prev_close <= 0 or open_px <= 0:
            return False
        # If open equals high, low, close and opened significantly higher than prev close (e.g. >= 4.5% for 5% band, or >= 9.5% for 10% band)
        is_frozen = (open_px == high_px == low_px == close_px) and (open_px > prev_close * 1.045)
        return is_frozen

    def is_lower_circuit(self, prev_close: float, open_px: float, high_px: float, low_px: float, close_px: float) -> bool:
        """Detects if a stock is locked at lower circuit (no buyers)."""
        if prev_close <= 0 or open_px <= 0:
            return False
        is_frozen = (open_px == high_px == low_px == close_px) and (open_px < prev_close * 0.955)
        return is_frozen

    def generate_orders(
        self,
        signal_date: str,
        exec_date: str,
        target_symbols: List[str],
        backup_candidates: List[str],
        is_risk_on: bool,
        market_data: Dict[str, Dict[str, float]]
    ) -> Dict[str, Any]:
        """
        Determines the exact set of BUY and SELL orders required for rebalance.
        market_data must contain {Symbol: {'PrevClose': ..., 'Open': ..., 'High': ..., 'Low': ..., 'Close': ...}}.
        """
        state = self.ledger.get_state()
        positions = self.ledger.get_positions()
        held_symbols = set(positions.keys())

        sell_orders = []
        buy_orders = []
        circuit_alerts = []

        # ------------------------------------------------------------------------------------------
        # STEP 1: RESOLVE PENDING EXITS FROM PREVIOUS DAYS
        # ------------------------------------------------------------------------------------------
        for sym, pos in positions.items():
            if pos.get("pending_exit", 0) == 1:
                p_data = market_data.get(sym, {})
                prev_c = p_data.get("PrevClose", pos["last_price"])
                open_p = p_data.get("Open", pos["last_price"])
                high_p = p_data.get("High", open_p)
                low_p = p_data.get("Low", open_p)
                close_p = p_data.get("Close", open_p)

                if self.is_lower_circuit(prev_c, open_p, high_p, low_p, close_p):
                    circuit_alerts.append(f"HOLDING {sym} STILL LOCKED IN LOWER CIRCUIT on {exec_date}. Retrying next open.")
                else:
                    sell_orders.append({
                        "symbol": sym,
                        "side": "SELL",
                        "shares": pos["shares"],
                        "indicative_price": open_p,
                        "reason": "PENDING_EXIT_RESOLVED"
                    })

        # ------------------------------------------------------------------------------------------
        # STEP 2: DETERMINE REBALANCE EXITS
        # ------------------------------------------------------------------------------------------
        if not is_risk_on:
            # Bear regime: liquidating all holdings to cash
            for sym, pos in positions.items():
                if pos.get("pending_exit", 0) == 1:
                    continue  # Already handled above
                p_data = market_data.get(sym, {})
                prev_c = p_data.get("PrevClose", pos["last_price"])
                open_p = p_data.get("Open", pos["last_price"])
                high_p = p_data.get("High", open_p)
                low_p = p_data.get("Low", open_p)
                close_p = p_data.get("Close", open_p)

                if self.is_lower_circuit(prev_c, open_p, high_p, low_p, close_p):
                    circuit_alerts.append(f"BEAR DEFENSE: {sym} LOCKED IN LOWER CIRCUIT on {exec_date}. Marked PENDING_EXIT.")
                    self.ledger.mark_pending_exit(sym, True)
                else:
                    sell_orders.append({
                        "symbol": sym,
                        "side": "SELL",
                        "shares": pos["shares"],
                        "indicative_price": open_p,
                        "reason": "BEAR_REGIME_CASH_DEFENSE"
                    })
        else:
            # Bull regime: sell positions that dropped out of target_symbols
            target_set = set(target_symbols)
            for sym in held_symbols:
                if sym not in target_set and positions[sym].get("pending_exit", 0) == 0:
                    pos = positions[sym]
                    p_data = market_data.get(sym, {})
                    prev_c = p_data.get("PrevClose", pos["last_price"])
                    open_p = p_data.get("Open", pos["last_price"])
                    high_p = p_data.get("High", open_p)
                    low_p = p_data.get("Low", open_p)
                    close_p = p_data.get("Close", open_p)

                    if self.is_lower_circuit(prev_c, open_p, high_p, low_p, close_p):
                        circuit_alerts.append(f"EXIT CANDIDATE {sym} LOCKED IN LOWER CIRCUIT on {exec_date}. Marked PENDING_EXIT.")
                        self.ledger.mark_pending_exit(sym, True)
                    else:
                        sell_orders.append({
                            "symbol": sym,
                            "side": "SELL",
                            "shares": pos["shares"],
                            "indicative_price": open_p,
                            "reason": "REBALANCE_EXIT"
                        })

        # ------------------------------------------------------------------------------------------
        # STEP 3: DETERMINE BUYS & SIZING (ONLY IF RISK-ON)
        # ------------------------------------------------------------------------------------------
        if is_risk_on:
            # Calculate estimated post-sell cash
            est_cash = state["current_cash"]
            for s in sell_orders:
                gross = s["shares"] * s["indicative_price"]
                slip = gross * config.SLIPPAGE_BPS
                dp = config.DP_CHARGE_PER_EXIT if config.ACTIVE_COST_MODEL == config.CostModel.PRODUCTION_REALISTIC else 0.0
                est_cash += (gross - slip - dp)

            # Calculate total portfolio value for equal-weight slot sizing
            retained_value = 0.0
            retained_symbols = set()
            for sym, pos in positions.items():
                if sym in target_symbols and pos.get("pending_exit", 0) == 0:
                    open_p = market_data.get(sym, {}).get("Open", pos["last_price"])
                    retained_value += (pos["shares"] * open_p)
                    retained_symbols.add(sym)

            est_total_equity = est_cash + retained_value
            target_slot_val = est_total_equity / float(config.NUM_STOCKS)

            # Available slots to fill
            candidate_pool = list(target_symbols) + list(backup_candidates)
            needed_new_slots = config.NUM_STOCKS - len(retained_symbols)
            
            filled_buys = 0
            pool_idx = 0
            available_cash_tracker = est_cash

            while filled_buys < needed_new_slots and pool_idx < len(candidate_pool):
                cand = candidate_pool[pool_idx]
                pool_idx += 1

                if cand in retained_symbols:
                    continue  # Already in portfolio

                p_data = market_data.get(cand, {})
                open_p = p_data.get("Open", 0.0)
                prev_c = p_data.get("PrevClose", 0.0)
                high_p = p_data.get("High", open_p)
                low_p = p_data.get("Low", open_p)
                close_p = p_data.get("Close", open_p)

                if open_p <= 0:
                    circuit_alerts.append(f"BUY CANDIDATE {cand} has invalid open price ({open_p}). Skipping.")
                    continue

                # Check upper-circuit freeze
                if self.is_upper_circuit(prev_c, open_p, high_p, low_p, close_p):
                    circuit_alerts.append(
                        f"BUY CANDIDATE {cand} LOCKED AT UPPER CIRCUIT on {exec_date} (Open: {open_p}, PrevClose: {prev_c}). "
                        f"Activating fallback to next candidate."
                    )
                    continue

                # Size integer shares
                alloc_cash = min(target_slot_val, available_cash_tracker)
                shares = int(alloc_cash / (open_p * (1.0 + config.SLIPPAGE_BPS)))
                if shares > 0:
                    cost = shares * open_p * (1.0 + config.SLIPPAGE_BPS)
                    available_cash_tracker -= cost
                    buy_orders.append({
                        "symbol": cand,
                        "side": "BUY",
                        "shares": shares,
                        "indicative_price": open_p,
                        "reason": "REBALANCE_ENTRY" if cand in target_symbols else "CIRCUIT_FALLBACK_ENTRY"
                    })
                    retained_symbols.add(cand)
                    filled_buys += 1

        return {
            "signal_date": signal_date,
            "exec_date": exec_date,
            "is_risk_on": is_risk_on,
            "sells": sell_orders,
            "buys": buy_orders,
            "circuit_alerts": circuit_alerts
        }

    def execute_plan(
        self,
        order_plan: Dict[str, Any],
        cost_model: config.CostModel = config.ACTIVE_COST_MODEL
    ) -> Dict[str, Any]:
        """
        Executes the generated order plan in PAPER mode (mutating SQLite ledger)
        OR generates human-readable staging tickets in LIVE_SIGNALS mode.
        """
        exec_date = order_plan["exec_date"]
        sells = order_plan["sells"]
        buys = order_plan["buys"]

        if self.mode == config.TradingMode.PAPER:
            logger.info(f"Executing PAPER rebalance for {exec_date}...")
            # 1. Execute all sells first to free cash
            for s in sells:
                self.ledger.execute_sell(
                    date_str=exec_date,
                    symbol=s["symbol"],
                    shares=s["shares"],
                    price=s["indicative_price"],
                    reason=s["reason"],
                    cost_model=cost_model
                )
            # 2. Execute buys
            for b in buys:
                self.ledger.execute_buy(
                    date_str=exec_date,
                    symbol=b["symbol"],
                    shares=b["shares"],
                    price=b["indicative_price"],
                    reason=b["reason"],
                    cost_model=cost_model
                )

            return {
                "status": "EXECUTED",
                "mode": self.mode.value,
                "sells_executed": len(sells),
                "buys_executed": len(buys)
            }
        else:
            # LIVE_SIGNALS mode: Generate staged broker ticket without executing
            ticket_text, ticket_path = self._generate_broker_ticket(order_plan)
            logger.info(f"Generated pre-market broker execution ticket at {ticket_path}")
            return {
                "status": "STAGED",
                "mode": self.mode.value,
                "ticket_path": ticket_path,
                "ticket_text": ticket_text,
                "sells_count": len(sells),
                "buys_count": len(buys)
            }

    def _generate_broker_ticket(self, order_plan: Dict[str, Any]) -> Tuple[str, str]:
        """Formats and saves copy-paste order ticket for Zerodha / Groww manual entry."""
        exec_date = order_plan["exec_date"]
        signal_date = order_plan["signal_date"]
        is_risk_on = order_plan["is_risk_on"]
        sells = order_plan["sells"]
        buys = order_plan["buys"]
        alerts = order_plan["circuit_alerts"]

        lines = [
            "=" * 80,
            f"             PRE-MARKET LIVE EXECUTION ORDER TICKET ({exec_date} 09:15 AM)",
            f"Signal Date: {signal_date} Close | Regime: {'BULL (RISK-ON)' if is_risk_on else 'BEAR (CASH DEFENSE)'}",
            f"Trading Mode: LIVE_SIGNALS (Manual Broker Entry) | Total Orders: {len(sells) + len(buys)}",
            "=" * 80,
            f"{'ACTION':<6} | {'SYMBOL':<14} | {'QTY':>6} | {'INDICATIVE PX':>14} | {'EST VALUE':>14} | {'PRODUCT':<7} | {'ORDER TYPE':<10}",
            "-" * 80
        ]

        total_sell_val = 0.0
        for s in sells:
            val = s["shares"] * s["indicative_price"]
            total_sell_val += val
            lines.append(
                f"{s['side']:<6} | {s['symbol']:<14} | {s['shares']:>6} | Rs {s['indicative_price']:>11.2f} | Rs {val:>11.2f} | {'CNC':<7} | {'MARKET/AMO':<10}"
            )

        total_buy_val = 0.0
        for b in buys:
            val = b["shares"] * b["indicative_price"]
            total_buy_val += val
            lines.append(
                f"{b['side']:<6} | {b['symbol']:<14} | {b['shares']:>6} | Rs {b['indicative_price']:>11.2f} | Rs {val:>11.2f} | {'CNC':<7} | {'MARKET/AMO':<10}"
            )

        lines.extend([
            "-" * 80,
            f"Est. Sell Proceeds: Rs {total_sell_val:,.2f} | Est. Buy Allocation: Rs {total_buy_val:,.2f}",
            "=" * 80
        ])

        if alerts:
            lines.append("\n[CIRCUIT / MICROSTRUCTURE ALERTS]:")
            for a in alerts:
                lines.append(f" - {a}")
            lines.append("=" * 80)

        ticket_text = "\n".join(lines)
        ticket_file = os.path.join(config.REPORTS_DIR, f"order_ticket_{exec_date}.txt")
        with open(ticket_file, "w", encoding="utf-8") as f:
            f.write(ticket_text)

        # Also save structured JSON
        json_file = os.path.join(config.REPORTS_DIR, f"order_ticket_{exec_date}.json")
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(order_plan, f, indent=2)

        return ticket_text, ticket_file
