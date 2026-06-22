"""交易日曆工具：查詢前一交易日、最近 N 個交易日、交易日判斷。

休市日（國定假日）來源為 market_holidays 表，由 HolidayCollector 定期抓 TWSE 假日表
寫入。本模組以模組層快取讀取一次，供 is_trading_day 判斷；抓取後呼叫
refresh_holiday_cache() 讓快取生效。表不存在或讀取失敗時退回「僅排除週末」。
"""

import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# 休市日快取（YYYY-MM-DD 集合）。None 表示尚未載入，首次查詢時 lazy load。
_holiday_cache: set[str] | None = None


def _load_holidays_from_db(db_path: str | None = None) -> set[str]:
    """從 market_holidays 讀取休市日集合。表不存在或讀取失敗回空集合（退回僅週末）。"""
    from db.connection import get_connection

    try:
        with get_connection(db_path) as conn:
            rows = conn.execute("SELECT date FROM market_holidays").fetchall()
        return {r[0] for r in rows}
    except Exception as e:
        logger.warning("載入休市日失敗，退回僅排除週末：%s", e)
        return set()


def refresh_holiday_cache(db_path: str | None = None) -> int:
    """重新從 DB 載入休市日快取，回傳載入筆數。抓取休市日後或測試時呼叫。"""
    global _holiday_cache
    _holiday_cache = _load_holidays_from_db(db_path)
    logger.info("休市日快取更新：%d 筆", len(_holiday_cache))
    return len(_holiday_cache)


def _holidays() -> set[str]:
    """取得休市日快取，未載入則 lazy load。"""
    global _holiday_cache
    if _holiday_cache is None:
        _holiday_cache = _load_holidays_from_db()
    return _holiday_cache


def is_market_holiday(date: str) -> bool:
    """判斷 date 是否為台股休市日（國定假日／補假／無交易日）。"""
    return date in _holidays()


def is_trading_day(date: str) -> bool:
    """判斷是否為交易日：排除週末，且非 market_holidays 中的休市日。"""
    dt = datetime.strptime(date, "%Y-%m-%d")
    if dt.weekday() >= 5:
        return False
    return not is_market_holiday(date)


def iter_trading_days(start_date: str, end_date: str) -> list[str]:
    """產生 start_date ~ end_date（含）之間的所有交易日，從舊到新。"""
    result: list[str] = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while dt <= end:
        d = dt.strftime("%Y-%m-%d")
        if dt.weekday() < 5 and not is_market_holiday(d):
            result.append(d)
        dt += timedelta(days=1)
    return result


def get_next_trading_day(date: str) -> str:
    """回傳 date 之後的下一個交易日（YYYY-MM-DD）。跳過週末與休市日。"""
    dt = datetime.strptime(date, "%Y-%m-%d")
    for _ in range(20):
        dt += timedelta(days=1)
        d = dt.strftime("%Y-%m-%d")
        if dt.weekday() < 5 and not is_market_holiday(d):
            return d
    return date  # 理論上不會走到


def get_previous_trading_day(
    date: str, conn: sqlite3.Connection | None = None
) -> str | None:
    """回傳 date 的前一個交易日（YYYY-MM-DD）。跳過週末和假日。

    有 conn 時取 raw_futures/raw_institutional 最近一筆「交易日」資料作基準；以
    is_trading_day 過濾，避免休市日混入的空殼 row（如休市日寫入的除息預設值）
    被當成前一交易日。
    """
    if conn is not None:
        for table in ("raw_futures", "raw_institutional"):
            rows = conn.execute(
                f"SELECT date FROM {table} WHERE date < ? "
                "ORDER BY date DESC LIMIT 30",
                (date,),
            ).fetchall()
            for (d,) in rows:
                if is_trading_day(d):
                    return d

    # Fallback: skip weekends and holidays, max 20 days back
    dt = datetime.strptime(date, "%Y-%m-%d")
    for _ in range(20):
        dt -= timedelta(days=1)
        d = dt.strftime("%Y-%m-%d")
        if dt.weekday() < 5 and not is_market_holiday(d):
            return d
    return None


def get_recent_trading_days(
    date: str, n: int, conn: sqlite3.Connection | None = None
) -> list[str]:
    """回傳 date 之前的 n 個交易日（不含 date），最新的排前面。"""
    if conn is not None:
        # 多取一些再用 is_trading_day 過濾掉混入的休市日/週末空殼 row，取前 n 筆
        rows = conn.execute(
            "SELECT date FROM raw_futures WHERE date < ? ORDER BY date DESC LIMIT ?",
            (date, n * 3 + 10),
        ).fetchall()
        filtered = [r[0] for r in rows if is_trading_day(r[0])]
        if filtered:
            return filtered[:n]

    # Fallback: skip weekends and holidays
    result: list[str] = []
    dt = datetime.strptime(date, "%Y-%m-%d")
    for _ in range(n * 5):
        dt -= timedelta(days=1)
        d = dt.strftime("%Y-%m-%d")
        if dt.weekday() < 5 and not is_market_holiday(d):
            result.append(d)
        if len(result) == n:
            break
    return result
