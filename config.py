"""
====================================================================================================
CONFIGURATION & STRATEGY PARAMETER SPECIFICATION
Central configuration module for Autonomous Momentum Paper Trader.
Matches canonical frozen parameter vector theta* exactly.
====================================================================================================
"""

import os
from enum import Enum

class CostModel(str, Enum):
    BACKTEST_EXACT = "BACKTEST_EXACT"        # 20 bps flat, ₹0 DP fee (bit-for-bit backtest parity)
    PRODUCTION_REALISTIC = "PRODUCTION_REAL" # 20 bps slippage + ₹15.93 DP charge per exit + STT

class TradingMode(str, Enum):
    PAPER = "PAPER"               # 100% autonomous simulation
    LIVE_SIGNALS = "LIVE_SIGNALS" # Generates exact pre-market order tickets for manual broker execution

# ==================================================================================================
# STRATEGY PARAMETERS (FROZEN THETA*)
# ==================================================================================================
NUM_STOCKS: int = 12
REBALANCE_CADENCE: int = 21                       # Trading days
BREADTH_GATE_THRESHOLD: float = 0.45             # 45% of liquid universe > 200 SMA
MIN_6M_MOMENTUM_HURDLE: float = 0.10             # ret_6m > +10%
MIN_DAILY_TURNOVER_VALUE: float = 10000000.0     # ₹1,00,00,000 (₹1 Crore) daily turnover
EXECUTION_LATENCY: str = "T+2"                   # Conservative 40-hour buffer

# Friction & Yield
ANNUAL_CASH_YIELD: float = 0.06                  # 6.0% p.a.
TRADING_DAYS_PER_YEAR: float = 250.0             # Daily compounding base
DAILY_LIQUID_RATE: float = (1.0 + ANNUAL_CASH_YIELD) ** (1.0 / TRADING_DAYS_PER_YEAR) - 1.0
SLIPPAGE_BPS: float = 0.0020                     # 20 bps commission + slippage
DP_CHARGE_PER_EXIT: float = 15.93                # CDSL/NSDL + GST depository charge

# Capital & Cash Flow Defaults
DEFAULT_INITIAL_CAPITAL: float = 500000.0        # ₹5,00,000 (Sweet spot)
DEFAULT_MONTHLY_SIP: float = 25000.0             # ₹25,000/month

# Benchmark Feed for Governance (Nippon India Nifty Midcap 150 ETF, TER: 0.23%)
BENCHMARK_SYMBOL: str = "MID150BEES"

# ==================================================================================================
# FORWARD GOVERNANCE KILL-SWITCH THRESHOLDS
# ==================================================================================================
HARD_MAX_DRAWDOWN_LIMIT: float = 0.30            # 30.0% max drawdown circuit breaker
MIN_CALMAR_RATIO_24M: float = 0.70               # 24-month rolling Calmar floor vs benchmark
MAX_CASH_STAGNATION_MONTHS: int = 18             # 18 consecutive months cash trigger
MAX_REALIZED_SLIPPAGE: float = 0.0040            # 40 bps capacity ceiling

# ==================================================================================================
# ACTIVE SYSTEM SETTINGS
# ==================================================================================================
ACTIVE_COST_MODEL: CostModel = CostModel.PRODUCTION_REALISTIC
ACTIVE_TRADING_MODE: TradingMode = TradingMode.PAPER

# File & Directory Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "paper_portfolio.db")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
REFERENCE_CA_PATH = os.path.join(BASE_DIR, "reference_corporate_actions.csv")
MASTER_PARQUET_PATH = os.getenv("MASTER_PARQUET_PATH", os.path.join(BASE_DIR, "recent_nse_cache.parquet"))
CACHE_PARQUET_PATH = os.path.join(BASE_DIR, "recent_nse_cache.parquet")
