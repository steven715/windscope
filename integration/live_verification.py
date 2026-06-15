"""盤中即時驗證：用即時加權指數對早上的訊號做雙基準比對。

唯讀觀察，不寫入 premarket.db。MIS 的 y(昨收)/o(開盤)/z(即時) 三欄即雙基準輸入，
故完全自包含。盤中連續呼叫，收盤時自然收斂到 verify_close 的結果。
"""

import json
import logging
import sqlite3
from datetime import datetime

# is_market_open 從 live_tracker 重新匯出，保持既有 import 路徑相容
from integration.live_tracker import get_cached_quote, is_market_open  # noqa: F401
from integration.verification import classify_against_benchmarks

logger = logging.getLogger(__name__)


def get_live_verification(
    date: str,
    conn: sqlite3.Connection,
    quote: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """回傳 date 當日盤中即時驗證資料。

    quote 可注入（測試用）；未提供時讀背景快取（不在請求路徑上打 MIS）。
    無當日訊號或無快取報價時回退化結果（has_signal / quote 為對應狀態）。
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

    as_of = None
    if quote is None:
        quote, as_of = get_cached_quote()

    base = {
        "date": date,
        "has_signal": True,
        "predicted_direction": direction,
        "confidence": confidence,
        "reasons": reasons,
        "market_open": market_open,
        "as_of": as_of,
    }

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
