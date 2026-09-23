"""
====================================================================================================
NSE TRADING CALENDAR & T+2 DETERMINISTIC DATE ARITHMETIC
Provides official NSE holiday schedules, trading day validation, anchored 21-day cadence,
and formal T+2 signal-to-execution mapping.
====================================================================================================
"""

import datetime
import os
import csv
from typing import Dict, List, Set, Tuple, Optional

# Official NSE Trading Holidays for 2026 and 2027 (Equities Segment)
DEFAULT_NSE_HOLIDAYS: Set[str] = {
    # 2026 Official NSE Trading Holidays (Equities Segment - Weekdays)
    "2026-01-15",  # Municipal Corporation Election - Maharashtra (Thursday)
    "2026-01-26",  # Republic Day (Monday)
    "2026-03-03",  # Holi (Tuesday)
    "2026-03-26",  # Shri Ram Navami (Thursday)
    "2026-03-31",  # Shri Mahavir Jayanti (Tuesday)
    "2026-04-03",  # Good Friday (Friday)
    "2026-04-14",  # Dr. Baba Saheb Ambedkar Jayanti (Tuesday)
    "2026-05-01",  # Maharashtra Day (Friday)
    "2026-05-28",  # Bakri Id (Thursday)
    "2026-06-26",  # Muharram (Friday)
    "2026-09-14",  # Ganesh Chaturthi (Monday)
    "2026-10-02",  # Mahatma Gandhi Jayanti (Friday)
    "2026-10-20",  # Dussehra (Tuesday)
    "2026-11-10",  # Diwali Balipratipada (Tuesday)
    "2026-11-24",  # Gurunanak Jayanti (Tuesday)
    "2026-12-25",  # Christmas (Friday)
    # 2027 Projected NSE Holidays (Subject to official circular late 2026)
    "2027-01-26",  # Republic Day (Tuesday)
    "2027-03-08",  # Mahashivratri (Monday)
    "2027-03-22",  # Holi (Monday)
    "2027-03-26",  # Good Friday (Friday)
    "2027-04-14",  # Dr. Baba Saheb Ambedkar Jayanti (Wednesday)
    "2027-05-01",  # Maharashtra Day (Saturday)
    "2027-10-02",  # Mahatma Gandhi Jayanti (Saturday)
    "2027-10-11",  # Dussehra (Monday)
    "2027-11-13",  # Gurunanak Jayanti (Saturday)
    "2027-12-25",  # Christmas (Saturday)
}

NSE_HOLIDAYS: Set[str] = set(DEFAULT_NSE_HOLIDAYS)
HOLIDAY_DETAILS: Dict[str, str] = {}

def _load_reference_holidays():
    """Dynamically loads holidays from reference_nse_holidays.csv if present."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ref_file = os.path.join(base_dir, "reference_nse_holidays.csv")
    if os.path.exists(ref_file):
        try:
            with open(ref_file, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    d = row.get("Date", "").strip()
                    occ = row.get("Occasion", "").strip()
                    if d:
                        NSE_HOLIDAYS.add(d)
                        HOLIDAY_DETAILS[d] = occ
        except Exception:
            pass

_load_reference_holidays()


def parse_date(date_str: str) -> datetime.date:
    """Parses 'YYYY-MM-DD' string into datetime.date."""
    return datetime.date(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:10]))


def format_date(dt: datetime.date) -> str:
    """Formats datetime.date into 'YYYY-MM-DD' string."""
    return dt.strftime("%Y-%m-%d")


def is_trading_day(dt: datetime.date) -> bool:
    """Returns True if dt is a Monday-Friday non-holiday trading day."""
    if dt.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    date_str = format_date(dt)
    return date_str not in NSE_HOLIDAYS


def next_trading_day(dt: datetime.date) -> datetime.date:
    """Returns the immediately following valid exchange trading day."""
    cur = dt + datetime.timedelta(days=1)
    while not is_trading_day(cur):
        cur += datetime.timedelta(days=1)
    return cur


def prev_trading_day(dt: datetime.date) -> datetime.date:
    """Returns the immediately preceding valid exchange trading day."""
    cur = dt - datetime.timedelta(days=1)
    while not is_trading_day(cur):
        cur -= datetime.timedelta(days=1)
    return cur


def get_trading_days_range(start_date: str, end_date: str) -> List[str]:
    """Generates all sequential valid trading days between start_date and end_date (inclusive)."""
    start_dt = parse_date(start_date)
    end_dt = parse_date(end_date)
    days = []
    cur = start_dt
    while cur <= end_dt:
        if is_trading_day(cur):
            days.append(format_date(cur))
        cur += datetime.timedelta(days=1)
    return days


def get_t2_execution_date(signal_date: str) -> str:
    """
    Computes strict T+2 execution date from Signal Date:
    - Signal Date: Day T-2 Close (e.g. Monday Close)
    - Staging Date: Day T-1 EOD (e.g. Tuesday 6:45 PM)
    - Execution Date: Day T Open (e.g. Wednesday 9:15 AM)
    """
    t_minus_2 = parse_date(signal_date)
    if not is_trading_day(t_minus_2):
        raise ValueError(f"Signal date {signal_date} is not an active exchange trading day.")
    t_minus_1 = next_trading_day(t_minus_2)
    t_exec = next_trading_day(t_minus_1)
    return format_date(t_exec)


def get_anchored_rebalance_schedule(inception_date: str, horizon_days: int = 500, cadence: int = 21) -> List[str]:
    """
    Generates deterministic rebalancing dates anchored to the exchange trading day sequence.
    Rebalance dates occur exactly every `cadence` (21) trading days from inception: T_0, T_21, T_42, ...
    """
    cur_dt = parse_date(inception_date)
    if not is_trading_day(cur_dt):
        cur_dt = next_trading_day(cur_dt)
        
    trading_days = []
    check_dt = cur_dt
    for _ in range(horizon_days):
        if is_trading_day(check_dt):
            trading_days.append(format_date(check_dt))
        check_dt += datetime.timedelta(days=1)
        
    rebal_dates = [trading_days[i] for i in range(0, len(trading_days), cadence)]
    return rebal_dates


def trading_days_between(start_date: str, end_date: str) -> int:
    """Counts the exact number of active trading days between two dates."""
    s_dt = parse_date(start_date)
    e_dt = parse_date(end_date)
    if s_dt > e_dt:
        return 0
    count = 0
    cur = s_dt + datetime.timedelta(days=1)
    while cur <= e_dt:
        if is_trading_day(cur):
            count += 1
        cur += datetime.timedelta(days=1)
    return count


def add_holiday(date_str: str, occasion: str) -> None:
    """Dynamically registers a new NSE holiday and persists it to reference_nse_holidays.csv."""
    dt = parse_date(date_str)
    day_name = dt.strftime("%A")
    year = str(dt.year)
    NSE_HOLIDAYS.add(date_str)
    HOLIDAY_DETAILS[date_str] = occasion

    base_dir = os.path.dirname(os.path.abspath(__file__))
    ref_file = os.path.join(base_dir, "reference_nse_holidays.csv")
    try:
        # Check if already present
        exists = False
        if os.path.exists(ref_file):
            with open(ref_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith(date_str):
                        exists = True
                        break
        if not exists:
            with open(ref_file, "a", encoding="utf-8") as f:
                f.write(f"{date_str},{occasion},{year},{day_name}\n")
    except Exception as e:
        pass


def get_upcoming_holidays(from_date: str, days_ahead: int = 45) -> List[Tuple[str, str, str]]:
    """Returns list of (Date, DayOfWeek, Occasion) for holidays occurring in the next N calendar days."""
    from_dt = parse_date(from_date)
    end_dt = from_dt + datetime.timedelta(days=days_ahead)
    upcoming = []
    for h_str in sorted(list(NSE_HOLIDAYS)):
        h_dt = parse_date(h_str)
        if from_dt <= h_dt <= end_dt:
            weekday = h_dt.strftime("%A")
            occ = HOLIDAY_DETAILS.get(h_str, "Exchange Holiday")
            upcoming.append((h_str, weekday, occ))
    return upcoming


class TradingCalendar:
    """Convenience class wrapper for NSE trading calendar arithmetic."""
    @staticmethod
    def parse_date(date_str: str) -> datetime.date:
        return parse_date(date_str)

    @staticmethod
    def format_date(dt: datetime.date) -> str:
        return format_date(dt)

    @staticmethod
    def is_trading_day(dt: datetime.date) -> bool:
        return is_trading_day(dt)

    @staticmethod
    def next_trading_day(dt: datetime.date) -> datetime.date:
        return next_trading_day(dt)

    @staticmethod
    def prev_trading_day(dt: datetime.date) -> datetime.date:
        return prev_trading_day(dt)

    @staticmethod
    def get_trading_days_range(start_date: str, end_date: str) -> List[str]:
        return get_trading_days_range(start_date, end_date)

    @staticmethod
    def get_t2_execution_date(signal_date: str) -> str:
        return get_t2_execution_date(signal_date)

    @staticmethod
    def get_anchored_rebalance_schedule(inception_date: str, horizon_days: int = 500, cadence: int = 21) -> List[str]:
        return get_anchored_rebalance_schedule(inception_date, horizon_days, cadence)

    @staticmethod
    def trading_days_between(start_date: str, end_date: str) -> int:
        return trading_days_between(start_date, end_date)

    @staticmethod
    def add_holiday(date_str: str, occasion: str) -> None:
        add_holiday(date_str, occasion)

    @staticmethod
    def get_upcoming_holidays(from_date: str, days_ahead: int = 45) -> List[Tuple[str, str, str]]:
        return get_upcoming_holidays(from_date, days_ahead)

