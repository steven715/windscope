"""盤中即時驗證：用即時加權指數對早上的訊號做雙基準比對。

唯讀觀察，不寫入 premarket.db。MIS 的 y(昨收)/o(開盤)/z(即時) 三欄即雙基準輸入，
故完全自包含。盤中連續呼叫，收盤時自然收斂到 verify_close 的結果。
"""

import json
import logging
import sqlite3
from datetime import datetime

from collectors.mis import MISCollector
from integration.verification import classify_against_benchmarks

logger = logging.getLogger(__name__)

# 台股現貨交易時段（含試撮前後略寬鬆）
_TRADING_START = (9, 0)
_TRADING_END = (13, 30)


def is_market_open(now: datetime | None = None) -> bool:
    """是否在台股現貨交易時段內（交易日 09:00–13:30）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return _TRADING_START <= (now.hour, now.minute) <= _TRADING_END


def get_live_verification(
    date: str,
    conn: sqlite3.Connection,
    quote: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """回傳 date 當日盤中即時驗證資料。

    quote 可注入（測試用）；未提供時向 MIS 抓加權指數 t00。
    無當日訊號或 MIS 失敗時回退化結果（has_signal / quote 為對應狀態）。
    """
    market_open = is_market_open(now)

    row = conn.execute(
        "SELECT direction, confidence, reasons FROM signals WHERE date = ?",
        (date,),
    ).fetchone()
    if row is None:
        return {"date": date, "has_signal": False, "market_open": market_open}

    direction, confidence, reasons_json = row
    reasons = json.loads(reasons_json) if reasons_json else []

    base = {
        "date": date,
        "has_signal": True,
        "predicted_direction": direction,
        "confidence": confidence,
        "reasons": reasons,
        "market_open": market_open,
    }

    if quote is None:
        quote = MISCollector().collect_index("t00")
    if quote is None:
        return {**base, "quote": None}

    # 開盤價缺失（盤前無成交）時 fallback 到即時價，避免跳空計算失敗
    open_price = quote["open"] if quote["open"] is not None else quote["price"]
    cls = classify_against_benchmarks(
        direction, quote["prev_close"], open_price, quote["price"])

    return {
        **base,
        "quote": quote,
        **cls,
        "hit_day_now": cls["hit_day"] == 1,
        "hit_open_now": cls["hit_open"] == 1,
    }
