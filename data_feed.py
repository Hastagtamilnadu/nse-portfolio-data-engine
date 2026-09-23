"""
====================================================================================================
RESILIENT NSE DATA FEED & BENCHMARK TRACKER
Downloads daily EOD Bhavcopy from NSE Archives, maintains local parquet cache,
fetches MID150BEES benchmark feed, and provides fast causal feature slices.
====================================================================================================
"""

import os
import time
import datetime
import logging
from typing import Dict, Optional, Tuple, List
import urllib.request
import urllib.error
import polars as pl

import config
import canonical_momentum_core as core
from trading_calendar import TradingCalendar

logger = logging.getLogger("DataFeed")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class DataFeed:
    """
    Manages EOD market data ingestion, cache reconciliation, and causal feature extraction.
    """

    def __init__(
        self,
        master_parquet_path: Optional[str] = None,
        cache_dir: Optional[str] = None
    ):
        self.master_parquet = master_parquet_path or config.MASTER_PARQUET_PATH
        self.cache_dir = cache_dir or os.path.join(config.BASE_DIR, "bhavcopy_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.calendar = TradingCalendar()

        # In-memory session headers mimicking standard browser
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": "https://www.nseindia.com/"
        }

    def _format_date_for_url(self, date_str: str) -> str:
        """Converts 'YYYY-MM-DD' to 'DDMMYYYY' for NSE URL."""
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d%m%Y")

    def download_nse_bhavcopy(self, date_str: str, max_retries: int = 3) -> Optional[pl.DataFrame]:
        """
        Downloads EOD Bhavcopy from NSE archives:
        URL: https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
        Implements exponential backoff retries and handles weekend/holiday absence.
        """
        ddmmyyyy = self._format_date_for_url(date_str)
        local_csv_path = os.path.join(self.cache_dir, f"sec_bhavdata_full_{ddmmyyyy}.csv")

        # If already cached locally as CSV, read directly
        if os.path.exists(local_csv_path) and os.path.getsize(local_csv_path) > 1024:
            logger.info(f"Loading cached Bhavcopy from {local_csv_path}")
            return self._parse_bhavcopy_csv(local_csv_path, date_str)

        url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
        logger.info(f"Fetching NSE Bhavcopy for {date_str} from {url}...")

        req = urllib.request.Request(url, headers=self.headers)
        for attempt in range(1, max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if resp.status == 200:
                        content = resp.read()
                        with open(local_csv_path, "wb") as f:
                            f.write(content)
                        logger.info(f"Successfully downloaded Bhavcopy for {date_str} ({len(content)} bytes)")
                        return self._parse_bhavcopy_csv(local_csv_path, date_str)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    logger.warning(f"Bhavcopy not available on NSE server for {date_str} (HTTP 404 - Holiday or not yet uploaded)")
                    self._check_stale_alert(date_str)
                    return None
                logger.warning(f"Attempt {attempt}/{max_retries} failed with HTTP {e.code}: {e.reason}")
            except Exception as e:
                logger.warning(f"Attempt {attempt}/{max_retries} failed: {e}")

            if attempt < max_retries:
                sleep_time = 2.0 ** attempt
                logger.info(f"Retrying in {sleep_time:.1f}s...")
                time.sleep(sleep_time)

        self._check_stale_alert(date_str)
        return None

    def _check_stale_alert(self, date_str: str) -> None:
        """Alerts if past 8:00 PM IST on a scheduled trading day and data is missing."""
        now = datetime.datetime.now()
        target_dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        today = now.date()

        if target_dt == today:
            if now.hour >= 20:
                logger.error(
                    f"[STALE DATA ALERT] Bhavcopy for trading day {date_str} is missing past 8:00 PM IST! "
                    f"Exchange upload may be delayed or network restricted."
                )

    def _parse_bhavcopy_csv(self, csv_path: str, date_str: str) -> Optional[pl.DataFrame]:
        """Parses raw NSE Bhavcopy CSV and standardizes columns to canonical schema."""
        try:
            df = pl.read_csv(csv_path, ignore_errors=True)
            # Standardize column headers: strip whitespace and uppercase
            col_map = {c: c.strip().upper() for c in df.columns}
            df = df.rename(col_map)

            # Map raw NSE headers to canonical columns
            rename_dict = {}
            if "SYMBOL" in df.columns: rename_dict["SYMBOL"] = "Symbol"
            if "SERIES" in df.columns: rename_dict["SERIES"] = "Series"
            if "OPEN_PRICE" in df.columns: rename_dict["OPEN_PRICE"] = "Open"
            if "HIGH_PRICE" in df.columns: rename_dict["HIGH_PRICE"] = "High"
            if "LOW_PRICE" in df.columns: rename_dict["LOW_PRICE"] = "Low"
            if "CLOSE_PRICE" in df.columns: rename_dict["CLOSE_PRICE"] = "Close"
            if "LAST_PRICE" in df.columns: rename_dict["LAST_PRICE"] = "Last"
            if "PREV_CLOSE" in df.columns: rename_dict["PREV_CLOSE"] = "PrevClose"
            if "TTL_TRD_QNTY" in df.columns: rename_dict["TTL_TRD_QNTY"] = "Volume"
            if "TTL_TRD_VAL" in df.columns: rename_dict["TTL_TRD_VAL"] = "Value"
            if "NO_OF_TRADES" in df.columns: rename_dict["NO_OF_TRADES"] = "Trades"
            if "ISIN" in df.columns: rename_dict["ISIN"] = "ISIN"

            df = df.rename(rename_dict)

            def clean_f(col):
                return pl.col(col).cast(pl.Utf8).str.strip_chars().str.replace_all(',', '').cast(pl.Float64, strict=False)

            def clean_i(col):
                return pl.col(col).cast(pl.Utf8).str.strip_chars().str.replace_all(',', '').cast(pl.Int64, strict=False)

            # In raw NSE Bhavcopy CSVs, turnover is reported in Lakhs as 'TURNOVER_LACS'
            if "Value" not in df.columns and "TURNOVER_LACS" in df.columns:
                df = df.with_columns(
                    (clean_f("TURNOVER_LACS") * 100000.0).alias("Value")
                )

            df = df.with_columns([
                pl.lit(date_str).alias("Date"),
                pl.col("Symbol").cast(pl.Utf8).str.strip_chars().alias("Symbol"),
                pl.col("Series").cast(pl.Utf8).str.strip_chars().alias("Series"),
                clean_f("Open").alias("Open"),
                clean_f("High").alias("High"),
                clean_f("Low").alias("Low"),
                clean_f("Close").alias("Close"),
                clean_f("PrevClose").alias("PrevClose"),
                clean_i("Volume").alias("Volume"),
                clean_f("Value").alias("Value"),
            ])

            required_cols = ["Date", "Symbol", "Series", "Open", "High", "Low", "Close", "PrevClose", "Volume", "Value"]
            for col in required_cols:
                if col not in df.columns:
                    raise ValueError(f"Missing required column {col} in bhavcopy {csv_path}")

            return df.select(required_cols)
        except Exception as e:
            logger.error(f"Error parsing bhavcopy CSV {csv_path}: {e}")
            return None

    def get_daily_bhavcopy(self, date_str: str) -> Optional[pl.DataFrame]:
        """
        Retrieves bhavcopy for date_str from master parquet first, then cache, then live download.
        """
        # Step 1: Check master parquet (predicate pushdown)
        if os.path.exists(self.master_parquet):
            sub = pl.scan_parquet(self.master_parquet).filter(pl.col("Date") == date_str).collect()
            if len(sub) > 0:
                return sub

        # Step 2: Download or load from cache
        return self.download_nse_bhavcopy(date_str)

    def get_price_lookup(self, date_str: str) -> Dict[str, Tuple[float, float]]:
        """Returns {Symbol: (PrevClose, Open)} dictionary for corporate action & open checking."""
        df = self.get_daily_bhavcopy(date_str)
        if df is None or len(df) == 0:
            return {}
        lookup = {}
        for row in df.select(["Symbol", "PrevClose", "Open"]).to_dicts():
            sym = row["Symbol"]
            prev_c = float(row["PrevClose"]) if row["PrevClose"] is not None else 0.0
            open_p = float(row["Open"]) if row["Open"] is not None else 0.0
            lookup[sym] = (prev_c, open_p)
        return lookup

    def get_closing_lookup(self, date_str: str) -> Dict[str, float]:
        """Returns {Symbol: Close} dictionary for portfolio mark-to-market."""
        df = self.get_daily_bhavcopy(date_str)
        if df is None or len(df) == 0:
            return {}
        lookup = {}
        for row in df.select(["Symbol", "Close"]).to_dicts():
            sym = row["Symbol"]
            c = float(row["Close"]) if row["Close"] is not None else 0.0
            lookup[sym] = c
        return lookup

    def get_benchmark_close(self, date_str: str) -> Optional[float]:
        """
        Returns Close price of MID150BEES (Nippon India Nifty Midcap 150 ETF).
        Note: TER 0.23% creates minor drag vs theoretical TRI.
        """
        closes = self.get_closing_lookup(date_str)
        if config.BENCHMARK_SYMBOL in closes:
            return closes[config.BENCHMARK_SYMBOL]

        # Fallback: scan master parquet specifically
        if os.path.exists(self.master_parquet):
            sub = pl.scan_parquet(self.master_parquet).filter(
                (pl.col("Date") <= date_str) & (pl.col("Symbol") == config.BENCHMARK_SYMBOL)
            ).sort("Date").collect()
            if len(sub) > 0:
                # Use latest known close up to date_str
                return float(sub.tail(1)["Close"].item())

        return None

    def get_recent_market_slice(self, eval_date: str, lookback_calendar_days: int = 500) -> pl.DataFrame:
        """
        Loads the trailing window of market data up to eval_date,
        computes causal factors, and returns the enriched dataframe.
        """
        eval_dt = datetime.datetime.strptime(eval_date, "%Y-%m-%d").date()
        start_dt = eval_dt - datetime.timedelta(days=lookback_calendar_days)
        start_str = start_dt.strftime("%Y-%m-%d")

        logger.info(f"Scanning market slice [{start_str} to {eval_date}] for factor computation...")
        df = pl.scan_parquet(self.master_parquet).filter(
            (pl.col("Date") >= start_str) & (pl.col("Date") <= eval_date)
        ).collect()

        # Check for any cached bhavcopy CSVs with dates beyond master parquet
        master_max_date = df["Date"].max() if len(df) > 0 else start_str
        extra_dfs = []
        if os.path.exists(self.cache_dir):
            for fname in sorted(os.listdir(self.cache_dir)):
                if fname.startswith("sec_bhavdata_full_") and fname.endswith(".csv"):
                    date_part = fname.replace("sec_bhavdata_full_", "").replace(".csv", "")
                    if len(date_part) == 8:
                        d_str = f"{date_part[4:]}-{date_part[2:4]}-{date_part[:2]}"
                        if d_str > master_max_date and d_str <= eval_date:
                            cached_df = self.get_daily_bhavcopy(d_str)
                            if cached_df is not None:
                                extra_dfs.append(cached_df)
        if extra_dfs:
            df = pl.concat([df] + extra_dfs).sort(["Symbol", "Date"])

        # Compute causal factors
        df = core.compute_causal_factors(df)
        return df
