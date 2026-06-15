"""盤中即時行情背景快取：背景排程定期抓 MIS 存入記憶體，頁面/API 只讀快取。

把「打 MIS 取即時報價」與「使用者請求」解耦：collect 走背景執行緒，
使用者開頁面只讀記憶體快取，瞬間返回、不被 HTTP 延遲卡住。
即時資料只進記憶體，不寫 premarket.db。
"""

import logging
from datetime import datetime

from collectors.mis import MISCollector

logger = logging.getLogger(__name__)

# 模組級記憶體快取（單一進程內共用）。as_of 為該筆報價的抓取時間 HH:MM:SS。
_cache: dict = {"quote": None, "as_of": None}

# 現貨交易時段（市場開盤判斷用）
_TRADING_START = (9, 0)
_TRADING_END = (13, 30)
# 背景刷新時段（較寬，涵蓋盤前試撮與剛收盤；此區間外不打網路）
_REFRESH_START = (8, 30)
_REFRESH_END = (14, 0)


def is_market_open(now: datetime | None = None) -> bool:
    """是否在台股現貨交易時段內（交易日 09:00–13:30）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return _TRADING_START <= (now.hour, now.minute) <= _TRADING_END


def _should_refresh(now: datetime) -> bool:
    """是否該打網路刷新：快取為空一律刷一次，否則僅在刷新時段內。"""
    if _cache["quote"] is None:
        return True
    if now.weekday() >= 5:
        return False
    return _REFRESH_START <= (now.hour, now.minute) <= _REFRESH_END


def refresh_live_quote(now: datetime | None = None) -> None:
    """背景排程呼叫：抓 MIS 加權指數即時報價存入快取。非刷新時段為 no-op。"""
    now = now or datetime.now()
    if not _should_refresh(now):
        return
    quote = MISCollector().collect_index("t00")
    if quote is not None:
        _cache["quote"] = quote
        _cache["as_of"] = now.strftime("%H:%M:%S")


def get_cached_quote() -> tuple[dict | None, str | None]:
    """回傳 (最新快取報價, 抓取時間)。尚無資料時為 (None, None)。"""
    return _cache["quote"], _cache["as_of"]


def _reset_cache() -> None:
    """測試用：清空快取。"""
    _cache["quote"] = None
    _cache["as_of"] = None
