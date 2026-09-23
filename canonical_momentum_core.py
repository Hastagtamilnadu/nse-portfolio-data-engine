"""
====================================================================================================
CANONICAL MOMENTUM CORE ENGINE (SINGLE SOURCE OF TRUTH)
Shared by the audited backtest and the autonomous paper/live trading system.
Enforces the frozen parameter vector theta* with 100% bit-for-bit mathematical parity.
====================================================================================================
"""

import datetime
from typing import List, Tuple, Dict, Any, Optional, Set
import polars as pl
import numpy as np
from scipy.optimize import brentq

# Valid discrete Indian corporate action factors (Splits & Bonuses)
VALID_FACTORS: List[float] = [2.0, 3.0, 4.0, 5.0, 10.0, 20.0]

# Canonical 6.0% p.a. cash yield compounded daily across 250 trading days
ANNUAL_CASH_YIELD: float = 0.06
TRADING_DAYS_PER_YEAR: float = 250.0
DAILY_LIQUID_RATE: float = (1.0 + ANNUAL_CASH_YIELD) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0

# Known historical corporate demergers requiring zero multiplier (K=1.0)
DEMERGER_EXCLUSIONS: Set = {
    ("INDIABULLS", "2007-01-02"),  # Indiabulls Real Estate (IBREL) demerger
    ("STAR", "2024-12-06"),        # Strides Pharma / OneSource Specialty Pharma demerger
}


def compute_causal_factors(df: pl.DataFrame) -> pl.DataFrame:
    """
    Computes trend and momentum factors with strict Shift(1) causal alignment (Zero Lookahead).
    Input df must contain: Symbol, Date, Open, High, Low, Close, PrevClose, Volume, Value.
    """
    df = df.with_columns([
        pl.col("Close").shift(1).rolling_mean(200).over("Symbol").alias("sma200"),
        (pl.col("Close").shift(1) / pl.col("Close").shift(125).over("Symbol") - 1.0).alias("ret_6m"),
        (pl.col("Close").shift(1) / pl.col("Close").shift(250).over("Symbol") - 1.0).alias("ret_12m"),
        pl.col("Close").shift(1).pct_change().rolling_std(60).over("Symbol").alias("vol60"),
        pl.col("Open").shift(-1).over("Symbol").alias("next_open")
    ])

    df = df.with_columns([
        (pl.col("Close") > pl.col("sma200")).cast(pl.Float64).alias("above_200"),
        ((0.5 * pl.col("ret_12m") + 0.5 * pl.col("ret_6m")) / 
         pl.when(pl.col("vol60") > 0).then(pl.col("vol60")).otherwise(0.02)).alias("mom_score")
    ])
    return df


def compute_market_breadth(df: pl.DataFrame) -> pl.DataFrame:
    """
    Computes 5-day smoothed market breadth strictly over the liquid universe:
    Liquid criteria: Value >= ₹10,000,000 (₹1 Crore daily turnover) AND Series in ['EQ', 'BE']
    AND non-null sma200.
    """
    breadth_df = df.filter(
        pl.col("sma200").is_not_null() &
        (pl.col("Value") >= 10000000) &
        (pl.col("Series").is_in(["EQ", "BE"]))
    ).group_by("Date").agg([
        pl.col("above_200").mean().alias("breadth_raw")
    ]).sort("Date").with_columns([
        pl.col("breadth_raw").rolling_mean(5).shift(1).alias("breadth_ma5")
    ]).with_columns([
        (pl.col("breadth_ma5") >= 0.45).alias("market_bull")
    ])
    return breadth_df


def should_be_risk_on(breadth_ma5: float) -> bool:
    """
    Single canonical call-site for the market breadth gate.
    Returns True (BULL regime) if breadth_ma5 >= 0.45, else False (BEAR cash defense).
    """
    return bool(breadth_ma5 >= 0.45)


def build_qualified_universe(df: pl.DataFrame, require_next_open: bool = True) -> pl.DataFrame:
    """
    Single canonical call-site enforcing all 4 qualification filters:
    1. Series == 'EQ'
    2. Close > SMA200
    3. Trailing 6-Month Return > +10.0% hurdle
    4. Daily Traded Value >= ₹1,00,00,000 (₹1 Crore)
    5. Valid non-null momentum score
    """
    cond = (
        (pl.col("Series") == "EQ") &
        (pl.col("Close") > pl.col("sma200")) &
        (pl.col("ret_6m") > 0.10) &
        (pl.col("Value") >= 10000000) &
        (pl.col("mom_score").is_not_null())
    )
    if require_next_open:
        cond = cond & (pl.col("next_open").is_not_null())
    return df.filter(cond)


def select_top_momentum(qualified_df: pl.DataFrame, signal_date: str, n: int = 12) -> List[str]:
    """
    Deterministic ranking and candidate selection:
    Sorts by mom_score descending, Symbol ascending (eliminates hash-seed non-determinism).
    Returns top n symbols.
    """
    sub = qualified_df.filter(pl.col("Date") == signal_date)
    if len(sub) == 0:
        return []
    top_moms = sub.sort(["mom_score", "Symbol"], descending=[True, False]).head(n)
    return top_moms["Symbol"].to_list()


def detect_corporate_action(
    prev_close: float, 
    open_px: float, 
    sym: str = "", 
    date_str: str = "", 
    exclude_demergers: bool = True
) -> float:
    """
    Sole canonical corporate action detector with exact mathematical tolerance floor:
    - Eliminates boundary bug: ratio < 1.70 (exact 15% tolerance floor for 2:1 split: 2.0 * 0.85 = 1.70).
    - Checks discrete factors: 2x, 3x, 4x, 5x, 10x, 20x within 15% tolerance.
    - Demergers excluded: returns K=1.0 so parent drop is absorbed as real holding loss.
    """
    if open_px <= 0 or prev_close <= 0:
        return 1.0
    if exclude_demergers and (sym, date_str) in DEMERGER_EXCLUSIONS:
        return 1.0
        
    ratio = prev_close / open_px
    # Exact mathematical floor: 2.0 * (1 - 0.15) = 1.70
    if ratio < 1.70:
        return 1.0
        
    nearest_k = round(ratio)
    if nearest_k in VALID_FACTORS:
        rel_diff = abs(ratio - nearest_k) / nearest_k
        if rel_diff <= 0.15 + 1e-9:
            return float(nearest_k)
            
    return 1.0


def accrue_daily_cash_yield(cash: float) -> float:
    """
    Calculates daily risk-free interest earned on idle cash (6.0% annualized over 250 trading days).
    """
    return cash * DAILY_LIQUID_RATE if cash > 0 else 0.0


def parse_date(s: str) -> datetime.date:
    return datetime.date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def calc_xirr(cash_flows: List[float], date_strings: List[str]) -> float:
    """
    Calculates true Money-Weighted Return (XIRR) using Brent's method on exact calendar dates.
    """
    if len(cash_flows) < 2:
        return 0.0
    t0 = parse_date(date_strings[0])
    years = [(parse_date(d) - t0).days / 365.25 for d in date_strings]
    
    def npv(r):
        return sum(c / ((1.0 + r) ** t) for c, t in zip(cash_flows, years))
        
    try:
        return float(brentq(npv, -0.50, 3.0) * 100.0)
    except Exception:
        return 0.0
