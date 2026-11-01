"""盤前判斷 → 契約 target（Insight ＋ Trade Plan），寫入 targets 表。

純導出：讀 signals（立場）與 raw_index（前一交易日高低點，失效條件用），依 settings 的
Playbook 參數組成契約 v0.2 的一筆 target。同一天重算得到同一個 target_id（冪等）；
已鎖定（locked_at）的 target 重算不會解除鎖定（db/targets.target_put 保證）。
"""

import hashlib
import json
import logging
import sqlite3

from config import settings
from db.targets import TargetLockedError, target_get, target_put
from utils.contract_schema import schema_version
from utils.trading_calendar import get_previous_trading_day
from utils.tw_time import now_tw_iso, tw_iso

logger = logging.getLogger(__name__)

STRATEGY_ID = "premarket_call"
_DIRECTION_SIGN = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}


def target_id_for(strategy_id: str, instrument_id: str, valid_from: str, rule_version: str) -> str:
    """契約冪等鍵的雜湊：sha256(strategy|instrument|valid_from|rule_version) 前 16 碼。"""
    key = "|".join((strategy_id, instrument_id, valid_from, rule_version))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _invalidation(date: str, direction: str, conn: sqlite3.Connection) -> dict:
    """失效條件：開盤 30 分內現貨跌破（偏多）／站上（偏空）前一交易日低／高點；無資料只留文字。"""
    if direction == "neutral":
        return {"text": "中性無部位，無失效條件"}
    prev = get_previous_trading_day(date)
    row = conn.execute("SELECT low, high FROM raw_index WHERE date = ?", (prev,)).fetchone() if prev else None
    until = tw_iso(date, settings.PREMARKET_INVALIDATION_UNTIL)
    if direction == "bullish":
        level, field, op, word = (row[0] if row else None), "low", "<", "跌破前一交易日低點"
    else:
        level, field, op, word = (row[1] if row else None), "high", ">", "站上前一交易日高點"
    text = f"開盤 {settings.PREMARKET_INVALIDATION_UNTIL} 前現貨{word}"
    if level is None:
        return {"text": text + "（前日高低點無資料，僅人工判定）"}
    return {"text": f"{text} {level:g}",
            "conditions": [{"instrument_id": "TWSE:TAIEX", "field": field, "op": op,
                            "value": float(level), "until": until}]}


def build_premarket_target(date: str, conn: sqlite3.Connection,
                           produced_at: str | None = None, force: bool = False) -> dict | None:
    """把 date 的盤前判斷轉成契約 target 並寫入 targets 表。無盤前判斷回 None。

    已鎖定且內容會變的 target 不覆蓋（記 WARNING、回傳既有那筆）；force=True 才覆蓋。
    """
    row = conn.execute(
        "SELECT direction, confidence, rule_version, reasons FROM signals WHERE date = ?", (date,)
    ).fetchone()
    if row is None:
        logger.warning("build_premarket_target: %s 無盤前判斷", date)
        return None
    direction, confidence, rule_version, reasons = row
    if direction not in _DIRECTION_SIGN:
        logger.warning("build_premarket_target: %s 方向不合法 %r", date, direction)
        return None
    confidence = int(confidence or 0)
    sign = _DIRECTION_SIGN[direction]
    weight = round(sign * confidence / settings.CONFIDENCE_MAX, 4) if sign else 0.0
    instrument = settings.PREMARKET_TARGET_INSTRUMENT
    valid_from = tw_iso(date, settings.PREMARKET_TARGET_VALID_FROM)
    pb = settings.PREMARKET_PLAYBOOK
    rv = rule_version or settings.SIGNAL_RULE_VERSION
    record = {
        "schema_version": schema_version("targets"),
        "target_id": target_id_for(STRATEGY_ID, instrument, valid_from, rv),
        "strategy_id": STRATEGY_ID,
        "rule_version": rv,
        "instrument_id": instrument,
        "target_weight": weight,
        "target_quantity": None,
        "conviction": round(confidence / settings.CONFIDENCE_MAX, 4) if confidence else None,
        "horizon": "intraday",
        "produced_at": produced_at or now_tw_iso(),
        "valid_from": valid_from,
        "valid_until": tw_iso(date, settings.PREMARKET_TARGET_VALID_UNTIL),
        "locked_at": None,
        "invalidation": _invalidation(date, direction, conn),
        "trade_plan": {
            "playbook_id": pb["playbook_id"], "playbook_version": pb["playbook_version"],
            "params": dict(pb["params"]), "max_entries_per_day": pb["max_entries_per_day"],
            "entry": dict(pb["entry"]), "risk": dict(pb["risk"]),
            "scaling": [dict(x) for x in pb["scaling"]], "exit": dict(pb["exit"]),
            "no_trade": list(pb["no_trade"]), "discretion_allowed": list(pb["discretion_allowed"]),
        },
        "reason": _reason_text(reasons),
        "inputs_ref": {"source_table": "signals", "key": {"date": date}, "inputs_hash": None},
    }
    try:
        target_put(conn, record, force=force)
    except TargetLockedError as e:
        logger.warning("build_premarket_target: %s；保留既有鎖定內容", e)
        return target_get(conn, record["target_id"])
    logger.info("target %s: %s %s weight=%s", record["target_id"], date, direction, weight)
    return record


def _reason_text(reasons: str | list | None) -> str | None:
    """signals.reasons（JSON list）→ 一句話（≤200 字）。"""
    if not reasons:
        return None
    try:
        items = json.loads(reasons) if isinstance(reasons, str) else list(reasons)
    except (TypeError, ValueError):
        return str(reasons)[:200]
    return "；".join(str(x) for x in items)[:200] or None
