"""休市日曆 collector：抓 TWSE OpenAPI 假日表，解析出台股休市日存入 market_holidays。

來源：https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule
回傳 JSON 陣列，每筆 {Name, Date, Weekday, Description}，Date 為民國年 YYYMMDD。
注意：表中混有「交易日通知」（如「國曆新年開始交易日」「農曆春節前最後交易日」），
這些是開市日不是休市日，必須排除；其餘皆為休市日（含補假、無交易僅結算）。
"""

import logging
from datetime import datetime

from config import settings
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"

# 名稱含這些字樣者為「交易日通知」（開市日），非休市，需排除。
_TRADING_NOTICE_KEYWORDS = ("開始交易", "最後交易")


def _roc_to_iso(roc_date: str) -> str | None:
    """民國年 YYYMMDD（如 '1150619'）轉西元 YYYY-MM-DD。格式異常回 None。"""
    s = (roc_date or "").strip()
    if len(s) != 7 or not s.isdigit():
        return None
    iso = f"{int(s[:3]) + 1911}-{s[3:5]}-{s[5:7]}"
    try:
        datetime.strptime(iso, "%Y-%m-%d")
    except ValueError:
        return None
    return iso


def parse_holidays(entries: list[dict]) -> list[dict]:
    """從 TWSE 假日表 entries 解析出休市日清單 [{date, name}]，date 為 YYYY-MM-DD。

    排除『開始交易/最後交易』這類交易日通知，只留實際休市日。日期解析失敗者略過。
    """
    out: list[dict] = []
    for e in entries or []:
        name = (e.get("Name") or "").strip()
        if any(k in name for k in _TRADING_NOTICE_KEYWORDS):
            continue
        iso = _roc_to_iso(e.get("Date", ""))
        if iso is None:
            logger.warning("holiday parse: 無法解析日期 %r (%s)", e.get("Date"), name)
            continue
        out.append({"date": iso, "name": name})
    return out


class HolidayCollector:
    """抓 TWSE 假日表，解析台股休市日，存入 market_holidays。"""

    def __init__(self, db_path: str | None = None):
        """db_path 可注入，預設從 settings 讀取。"""
        self.db_path = db_path or settings.DB_PATH

    def fetch(self) -> list[dict] | None:
        """抓並解析休市日清單。HTTP 或解析失敗回 None。"""
        try:
            resp = http_get(HOLIDAY_URL)
            entries = resp.json()
        except Exception as e:
            logger.error("HolidayCollector fetch 失敗: %s | URL: %s", e, HOLIDAY_URL)
            return None
        holidays = parse_holidays(entries)
        if not holidays:
            logger.warning("HolidayCollector: 解析後無休市日，回傳 None")
            return None
        return holidays

    def save(self, holidays: list[dict]) -> int:
        """以 source='twse_openapi' upsert 進 market_holidays，回傳寫入筆數。"""
        now = datetime.now().isoformat()
        with get_connection(self.db_path) as conn:
            for h in holidays:
                conn.execute(
                    "INSERT INTO market_holidays (date, name, source, fetched_at) "
                    "VALUES (?, ?, 'twse_openapi', ?) "
                    "ON CONFLICT(date) DO UPDATE SET "
                    "  name = excluded.name, source = excluded.source, "
                    "  fetched_at = excluded.fetched_at",
                    (h["date"], h["name"], now),
                )
        return len(holidays)

    def run(self) -> int:
        """fetch + save，回傳寫入筆數；失敗回 0（不 raise，符合 collector 慣例）。"""
        logger.info("HolidayCollector: starting")
        holidays = self.fetch()
        if not holidays:
            logger.warning("HolidayCollector: 無資料可存")
            return 0
        n = self.save(holidays)
        logger.info("HolidayCollector: saved %d holidays", n)
        return n
