"""個股歷史K線回補 + 趨勢分析：加入觀察名單時撈歷史、算當前趨勢。

流程：STOCK_DAY 逐月往回撈 raw_stock_daily（年線暖機需 ~13 個月）→ compute_trend_signal。
被 watchlist 新增（web 背景任務 / CLI）與 backfill-stock CLI 共用。
"""

import logging
from datetime import date as _date

from collectors.twse import TWSECollector
from config import settings
from db.connection import get_connection
from integration.trend_signal import compute_trend_signal

logger = logging.getLogger(__name__)

# 原始日K來源：證交所 STOCK_DAY（未還原股價）。還原不是另一個「來源」——而是回補後
# 由 price_adjust 自動處理（偵測分割/大除權缺口 + 免費配息因子回推），見 backfill_and_analyze。
SOURCE_TWSE = "twse"
_VALID_SOURCES = {SOURCE_TWSE}


def _months_back(n: int, today: _date | None = None) -> list[str]:
    """最近 n 個月的每月首日（YYYY-MM-01），新→舊。用於 STOCK_DAY 逐月定位查詢。"""
    today = today or _date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}-01")
        m -= 1
        if m == 0:
            y -= 1
            m = 12
    return out


def backfill_stock_history(stock_id: str, db_path: str | None = None,
                           months: int | None = None,
                           today: _date | None = None,
                           source: str = SOURCE_TWSE) -> int:
    """回補個股歷史日K（STOCK_DAY 逐月往回），回傳存入的 bar 總數。

    單月失敗（無資料/請求錯）記 log 後略過，不中斷其餘月份。
    source 目前僅支援 SOURCE_TWSE；其他值退回 twse 並記 warning。
    """
    if source not in _VALID_SOURCES:
        logger.warning("backfill_stock_history: 來源 %r 尚未支援，退回 %s",
                       source, SOURCE_TWSE)
        source = SOURCE_TWSE
    months = months or settings.TREND_BACKFILL_MONTHS
    collector = TWSECollector(db_path=db_path)
    total = 0
    for month_date in _months_back(months, today):
        try:
            bars = collector.collect_stock_ohlc_month(month_date, stock_id)
            if bars:
                total += collector.save_stock_ohlc(stock_id, bars)
        except Exception as e:
            logger.error("backfill_stock_history %s %s failed: %s",
                         stock_id, month_date, e)
    logger.info("backfill_stock_history %s: %d bars over %d months",
                stock_id, total, months)
    return total


def analyze_stock_trend(stock_id: str, db_path: str | None = None) -> dict | None:
    """依現有 raw_stock_daily 算個股當前趨勢訊號並寫入 stock_trend_signals。"""
    with get_connection(db_path) as conn:
        return compute_trend_signal(stock_id, conn)


def backfill_dividends(stock_id: str, db_path: str | None = None) -> int:
    """撈免費除權息還原因子並存入 stock_dividends（best-effort，失敗回 0 不擋流程）。"""
    try:
        from collectors.dividend import collect_dividend_factors, save_dividend_factors

        events = collect_dividend_factors(stock_id)
        if events:
            return save_dividend_factors(stock_id, events, db_path=db_path)
    except Exception as e:
        logger.warning("backfill_dividends %s failed: %s", stock_id, e)
    return 0


def backfill_foreign(stock_id: str, db_path: str | None = None) -> int:
    """撈個股外資買賣超歷史並存入 raw_chip __FOREIGN__（best-effort，失敗回 0）。

    籌碼的『外資動向』本質也是歷史資料計算，故可像 K線一樣回補（免費 FinMind）。
    """
    try:
        from collectors.institutional_stock import (
            collect_foreign_history,
            save_foreign_history,
        )

        rows = collect_foreign_history(stock_id)
        if rows:
            return save_foreign_history(stock_id, rows, db_path=db_path)
    except Exception as e:
        logger.warning("backfill_foreign %s failed: %s", stock_id, e)
    return 0


def backfill_and_analyze(stock_id: str, db_path: str | None = None,
                         months: int | None = None,
                         source: str = SOURCE_TWSE) -> tuple[int, dict | None]:
    """加入觀察名單時的完整流程：撈歷史K線 → 撈配息還原因子 → 分析當前趨勢（還原後）。

    回傳 (bar 數, 訊號 dict)。
    """
    n = backfill_stock_history(stock_id, db_path=db_path, months=months,
                               source=source)
    backfill_dividends(stock_id, db_path=db_path)
    backfill_foreign(stock_id, db_path=db_path)   # 籌碼外資動向：歷史也可回補（免費）
    result = analyze_stock_trend(stock_id, db_path=db_path)
    return n, result
