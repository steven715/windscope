"""休市日曆刷新 job：抓 TWSE 假日表寫入 DB，並刷新交易日曆快取。

非每日情報 job，屬基礎設施。由 scheduler 在啟動時與每月定期觸發
（一年/一月查一次即可，TWSE 一次給整年假日表）。
"""

import logging

from collectors.holiday import HolidayCollector
from utils.trading_calendar import refresh_holiday_cache

logger = logging.getLogger(__name__)


def run_refresh_holidays(db_path: str | None = None) -> dict:
    """抓最新休市日曆寫入 DB 並刷新快取。

    回傳 {status, fetched, cached}：fetched 為本次抓到筆數，cached 為快取總筆數。
    抓取失敗（fetched=0）時仍刷新快取（沿用 DB 既有資料），status='failed'。
    """
    fetched = HolidayCollector(db_path=db_path).run()
    cached = refresh_holiday_cache(db_path)
    status = "completed" if fetched > 0 else "failed"
    logger.info(
        "run_refresh_holidays: fetched=%d cached=%d status=%s",
        fetched, cached, status,
    )
    return {"status": status, "fetched": fetched, "cached": cached}
