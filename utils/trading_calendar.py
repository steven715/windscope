"""交易日曆工具：查詢前一交易日、最近 N 個交易日、交易日判斷。"""

import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def is_trading_day(date: str) -> bool:
    """判斷是否為交易日。目前只排除週末，國定假日暫不處理。"""
    dt = datetime.strptime(date, "%Y-%m-%d")
    return dt.weekday() < 5


def iter_trading_days(start_date: str, end_date: str) -> list[str]:
    """產生 start_date ~ end_date（含）之間的所有交易日，從舊到新。"""
    result: list[str] = []
    dt = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    while dt <= end:
        if dt.weekday() < 5:
            result.append(dt.strftime("%Y-%m-%d"))
        dt += timedelta(days=1)
    return result


def get_next_trading_day(date: str) -> str:
    """回傳 date 之後的下一個交易日（YYYY-MM-DD）。跳過週末。"""
    dt = datetime.strptime(date, "%Y-%m-%d")
    for _ in range(10):
        dt += timedelta(days=1)
        if dt.weekday() < 5:
            return dt.strftime("%Y-%m-%d")
    return date  # 理論上不會走到


def get_previous_trading_day(
    date: str, conn: sqlite3.Connection | None = None
) -> str | None:
    """回傳 date 的前一個交易日（YYYY-MM-DD）。跳過週末和假日。"""
    if conn is not None:
        row = conn.execute(
            "SELECT date FROM raw_futures WHERE date < ? ORDER BY date DESC LIMIT 1",
            (date,),
        ).fetchone()
        if row:
            return row[0]
        row = conn.execute(
            "SELECT date FROM raw_institutional WHERE date < ? ORDER BY date DESC LIMIT 1",
            (date,),
        ).fetchone()
        if row:
            return row[0]

    # Fallback: skip weekends, max 10 days back
    dt = datetime.strptime(date, "%Y-%m-%d")
    for _ in range(10):
        dt -= timedelta(days=1)
        if dt.weekday() < 5:
            return dt.strftime("%Y-%m-%d")
    return None


def get_recent_trading_days(
    date: str, n: int, conn: sqlite3.Connection | None = None
) -> list[str]:
    """回傳 date 之前的 n 個交易日（不含 date），最新的排前面。"""
    if conn is not None:
        rows = conn.execute(
            "SELECT date FROM raw_futures WHERE date < ? ORDER BY date DESC LIMIT ?",
            (date, n),
        ).fetchall()
        if rows:
            return [r[0] for r in rows]

    # Fallback: skip weekends
    result: list[str] = []
    dt = datetime.strptime(date, "%Y-%m-%d")
    for _ in range(n * 3):
        dt -= timedelta(days=1)
        if dt.weekday() < 5:
            result.append(dt.strftime("%Y-%m-%d"))
        if len(result) == n:
            break
    return result
