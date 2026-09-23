"""
====================================================================================================
CORPORATE ACTION WATCHER (REFERENCE TABLE BACKSTOP + CORE HEURISTIC FALLBACK)
Monitors held portfolio positions for stock splits, bonuses, and demergers.
Uses official reference table as primary authority, falling back to canonical core heuristic.
====================================================================================================
"""

import os
import csv
import logging
from typing import Dict, List, Optional, Tuple, Any

import config
import canonical_momentum_core as core

logger = logging.getLogger("CorporateActionWatcher")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class CorporateActionWatcher:
    """
    Guards against unadjusted split/bonus distortions and demerger traps in live trading.
    """

    def __init__(self, reference_csv_path: Optional[str] = None):
        self.reference_csv_path = reference_csv_path or config.REFERENCE_CA_PATH
        self.reference_actions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.load_reference_actions()

    def load_reference_actions(self) -> None:
        """Loads reference corporate actions from CSV into fast-lookup memory cache."""
        self.reference_actions.clear()
        if not os.path.exists(self.reference_csv_path):
            logger.warning(f"Reference CA file not found at {self.reference_csv_path}. Initializing empty.")
            return

        with open(self.reference_csv_path, mode="r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if not row or not row.get("Symbol"):
                    continue
                sym = row["Symbol"].strip().upper()
                ex_date = row["Ex_Date"].strip()
                action_type = row.get("Action_Type", "").strip().upper()
                try:
                    factor = float(row.get("Factor", 1.0))
                except ValueError:
                    factor = 1.0
                details = row.get("Details", "").strip()

                self.reference_actions[(sym, ex_date)] = {
                    "Symbol": sym,
                    "Ex_Date": ex_date,
                    "Action_Type": action_type,
                    "Factor": factor,
                    "Details": details
                }
        logger.info(f"Loaded {len(self.reference_actions)} reference corporate actions from {self.reference_csv_path}")

    def lookup_reference(self, symbol: str, ex_date: str) -> Optional[Dict[str, Any]]:
        """Checks if a corporate action is officially registered in reference table."""
        key = (symbol.strip().upper(), ex_date.strip())
        return self.reference_actions.get(key)

    def check_and_resolve_action(
        self, 
        symbol: str, 
        date_str: str, 
        prev_close: float, 
        open_px: float
    ) -> Tuple[float, str, str]:
        """
        Determines the corporate action factor and action type:
        Returns: (factor, action_type, source)
        Source is one of: 'REFERENCE_TABLE', 'CORE_HEURISTIC', 'NONE'
        """
        sym = symbol.strip().upper()
        date_clean = date_str.strip()

        # Step 1: Primary Backstop (Reference Table)
        ref = self.lookup_reference(sym, date_clean)
        if ref is not None:
            factor = float(ref["Factor"])
            action_type = ref["Action_Type"]
            logger.info(
                f"[CA RESOLVED - REFERENCE] {sym} on {date_clean}: "
                f"Action={action_type}, Factor={factor} ({ref.get('Details', '')})"
            )
            return factor, action_type, "REFERENCE_TABLE"

        # Step 2: Canonical Shared Core Heuristic Fallback
        factor = core.detect_corporate_action(
            prev_close=prev_close,
            open_px=open_px,
            sym=sym,
            date_str=date_clean,
            exclude_demergers=True
        )

        if factor > 1.0:
            action_type = f"SPLIT_OR_BONUS_{int(factor)}X"
            logger.info(
                f"[CA RESOLVED - HEURISTIC] {sym} on {date_clean}: "
                f"Ratio={prev_close / open_px:.2f} -> K={factor} ({action_type})"
            )
            return factor, action_type, "CORE_HEURISTIC"

        # Step 3: Unlisted severe overnight drop check (>25% drop)
        if prev_close > 0 and open_px > 0 and open_px <= prev_close * 0.75:
            drop_pct = ((prev_close - open_px) / prev_close) * 100.0
            logger.warning(
                f"[ALERT - UNLISTED DROP] Overnight drop of {drop_pct:.1f}% detected for {sym} on {date_clean} "
                f"(PrevClose: {prev_close:.2f}, Open: {open_px:.2f}). "
                f"Treated as normal market loss (K=1.0). If this is a corporate action, register it via 'trader_cli.py add-ca'."
            )

        return 1.0, "NONE", "NONE"

    def adjust_position(self, shares: int, entry_price: float, factor: float) -> Tuple[int, float]:
        """
        Adjusts held shares and entry price according to factor K.
        Keeps cost basis invariant: new_shares * new_entry_price ~= old_shares * old_entry_price.
        """
        if factor > 1.0:
            new_shares = int(shares * factor)
            new_entry_price = round(entry_price / factor, 4)
            return new_shares, new_entry_price
        return shares, entry_price

    def add_reference_action(
        self, 
        symbol: str, 
        ex_date: str, 
        action_type: str, 
        factor: float, 
        details: str = ""
    ) -> None:
        """Appends a confirmed corporate action to reference CSV and updates cache."""
        sym = symbol.strip().upper()
        ex_date_clean = ex_date.strip()
        action_clean = action_type.strip().upper()

        new_row = {
            "Symbol": sym,
            "Ex_Date": ex_date_clean,
            "Action_Type": action_clean,
            "Factor": str(factor),
            "Details": details.strip()
        }

        file_exists = os.path.exists(self.reference_csv_path)
        with open(self.reference_csv_path, mode="a", newline="", encoding="utf-8") as f:
            fieldnames = ["Symbol", "Ex_Date", "Action_Type", "Factor", "Details"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists or os.path.getsize(self.reference_csv_path) == 0:
                writer.writeheader()
            writer.writerow(new_row)

        self.reference_actions[(sym, ex_date_clean)] = {
            "Symbol": sym,
            "Ex_Date": ex_date_clean,
            "Action_Type": action_clean,
            "Factor": float(factor),
            "Details": details.strip()
        }
        logger.info(f"Appended confirmed corporate action for {sym} on {ex_date_clean} to {self.reference_csv_path}")

    def audit_and_adjust_holdings(
        self,
        positions: Dict[str, Dict[str, Any]],
        date_str: str,
        price_lookup: Dict[str, Tuple[float, float]]
    ) -> List[Dict[str, Any]]:
        """
        Scans currently held portfolio positions against daily open/prev_close.
        Applies any detected corporate action adjustments immediately in place.
        Returns a list of audit adjustment records for logging.
        """
        adjustments = []
        for sym, pos in list(positions.items()):
            if sym not in price_lookup:
                continue
            prev_close, open_px = price_lookup[sym]
            factor, action_type, source = self.check_and_resolve_action(sym, date_str, prev_close, open_px)

            if factor > 1.0:
                old_shares = pos["shares"]
                old_price = pos["entry_price"]
                new_shares, new_price = self.adjust_position(old_shares, old_price, factor)
                pos["shares"] = new_shares
                pos["entry_price"] = new_price

                adjustment_record = {
                    "Date": date_str,
                    "Symbol": sym,
                    "Action_Type": action_type,
                    "Factor": factor,
                    "Source": source,
                    "Old_Shares": old_shares,
                    "New_Shares": new_shares,
                    "Old_Entry_Price": old_price,
                    "New_Entry_Price": new_price
                }
                adjustments.append(adjustment_record)
                logger.info(
                    f"[HOLDING ADJUSTED] {sym}: Shares {old_shares} -> {new_shares}, "
                    f"Entry Price Rs {old_price:.2f} -> Rs {new_price:.2f}"
                )

        return adjustments
