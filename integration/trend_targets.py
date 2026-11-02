"""個股趨勢訊號（stock_trend_signals）→ 契約 target（strategy_id=trend_signal，Playbook trend_pullback p1）。

純導出、冪等：每個交易日收盤後，對 watchlist 每檔比對「上一筆 target 的批數」與「今天訊號說的動作」，
批數有變才發**一筆新 target**（weight＝累積批次權重；大勢離開 UP → weight 0）。規則與 integration/backtest.py
同一套（關注建倉區→首批、可加倉＋冷卻→加一批、減倉→減一批、大勢≠UP→全出；批數上限與注碼比例取
settings.BACKTEST_*），所以回測跑的就是 targets 會發的動作；改批次規則 bump BACKTEST_RULE_VERSION。
回測看不到的兩件事這裡多做：閘門 FAIL 時不開新倉也不加碼（只准減／出）；收盤已在失效價位之下不開新倉。
狀態的單一真相是 targets 表（上一筆 target 的 inputs_ref.key 記 units／last_add_date），不另存；只往前重放——
重算某一天時，那天已發的 target 會被覆蓋（批數變回不變＝覆蓋成「維持原批數」），更後面日期的 target 不會回頭改（記 WARNING）。
已鎖定的 target 重算不覆蓋（db/targets.target_put 保證）。conviction 不填：趨勢訊號的 big_trend_confidence 是年線斜率，不是 0–1 信心。
"""

import logging
import sqlite3

from config import settings
from db.instruments import instrument_get
from db.targets import TargetLockedError, target_get, target_put, targets_list
from db.watchlist import watchlist_list
from integration.backtest import tranche_weights
from integration.price_adjust import load_bars
from integration.targets_builder import target_id_for
from integration.trend_signal import GATE_FAIL, GATE_PASS, GATE_UNKNOWN, UP, TrendReading, _route, load_stock_trend
from utils.contract_schema import schema_version
from utils.trading_calendar import get_next_trading_day, iter_trading_days
from utils.tw_time import now_tw_iso, tw_iso

logger = logging.getLogger(__name__)

STRATEGY_ID = "trend_signal"


def max_units() -> int:
    """批數上限（與回測同：single 模式＝1）。"""
    return 1 if settings.BACKTEST_ENTRY_MODE == "single" else max(1, settings.BACKTEST_MAX_UNITS)


def cumulative_weight(units: int) -> float:
    """持有 units 批時的 target_weight（NAV 比例）＝前 units 批注碼和；0 批＝0。"""
    w = tranche_weights(max_units(), settings.BACKTEST_TRANCHE_SIZING)
    return round(sum(w[:max(0, min(units, len(w)))]), 4)


def decide_units(prev_units: int, big: str, action: str, position: str, days_since_add: int | None,
                 gate_fail: bool = False) -> tuple[int, str]:
    """今天訊號 → 明天該持有幾批（忠實對應 backtest.run_backtest 的決策分支；gate_fail 另擋開倉與加碼）。回傳 (units, 一句話)。"""
    if big != UP:
        return 0, f"大勢 {big}，離開 UP → 全數出場" if prev_units else f"大勢 {big}，不進場"
    if prev_units == 0:
        if action == "關注建倉區" and not gate_fail:
            return 1, "大勢 UP、回踩月線企穩 → 關注建倉區（第 1 批）"
        return 0, f"大勢 UP，{action}，不進場"
    trim = settings.BACKTEST_ENTRY_MODE != "single"
    if trim and position == "減倉":
        return prev_units - 1, f"超買遠離 → 減倉（{prev_units}→{prev_units - 1} 批）"
    if gate_fail:
        return prev_units, f"內功閘門 FAIL，不加碼，持有 {prev_units} 批"
    if (prev_units < max_units() and position == "可加倉" and action != "不宜追高"
            and (days_since_add is None or days_since_add >= settings.BACKTEST_ADD_COOLDOWN_DAYS)):
        return prev_units + 1, f"回檔企穩 → 可加倉（{prev_units}→{prev_units + 1} 批）"
    return prev_units, f"持有 {prev_units} 批不變"


def _effective_action(sig: TrendReading) -> str:
    """回測假設閘門放行（BACKTEST_ASSUME_GATE_PASS）：UNKNOWN 視為 PASS 重算 action_hint；FAIL 不改（由 gate_fail 擋）。"""
    if settings.BACKTEST_ASSUME_GATE_PASS and sig.fundamental_gate == GATE_UNKNOWN:
        return _route(sig.big_trend, sig.small_state, sig.small_turn_up, GATE_PASS)[0]
    return sig.action_hint


def _trend_targets(instrument: str, conn: sqlite3.Connection) -> list[dict]:
    """同商品的全部 trend_signal targets，依 (valid_from, produced_at) 升冪。"""
    rows = [t for t in targets_list(conn, strategy_id=STRATEGY_ID, limit=1_000_000) if t["instrument_id"] == instrument]
    return sorted(rows, key=lambda t: (t["valid_from"], t["produced_at"]))


def _days_since(last_add_date: str | None, date: str) -> int | None:
    """上次加碼到 date 隔了幾個交易日（以行事曆算，與回測的「隔幾根日 K」在無停牌時相同）；沒加過回 None。"""
    if not last_add_date:
        return None
    if last_add_date >= date:
        return 0
    return max(0, len(iter_trading_days(last_add_date, date)) - 1)


def _invalidation(instrument: str, value_center: float | None) -> tuple[dict, float | None]:
    """失效＝收盤跌破價值中樞（月線）TREND_TARGET_INVALIDATION_PCT% 以上；回 (invalidation, 失效價位)。"""
    pct = settings.TREND_TARGET_INVALIDATION_PCT
    text = f"收盤跌破月線 {pct:g}% 以上（小勢轉跌破恐慌）"
    if value_center is None:
        return {"text": text + "（月線無資料，僅人工判定）"}, None
    level = round(value_center * (1 - pct / 100), 2)
    return {"text": f"{text}：{level:g}",
            "conditions": [{"instrument_id": instrument, "field": "close", "op": "<", "value": level, "until": None}]}, level


def _trade_plan(close: float, level: float) -> dict:
    """以 settings.TREND_PLAYBOOK 為模板，停損距離＝收盤到失效價位（呼叫端已確認 close > level）。"""
    pb = settings.TREND_PLAYBOOK
    return {
        "playbook_id": pb["playbook_id"], "playbook_version": pb["playbook_version"],
        "params": dict(pb["params"]), "max_entries_per_day": pb["max_entries_per_day"],
        "entry": dict(pb["entry"]),
        "risk": {**pb["risk"], "stop_pts": round(close - level, 2), "max_units": max_units()},
        "scaling": [dict(x) for x in pb["scaling"]], "exit": dict(pb["exit"]),
        "no_trade": list(pb["no_trade"]), "discretion_allowed": list(pb["discretion_allowed"]),
    }


def _close_on(date: str, stock_id: str, conn: sqlite3.Connection) -> float | None:
    bars = load_bars(stock_id, conn, asof=date)
    return bars[-1]["close"] if bars and bars[-1]["date"] == date else None


def build_trend_target(date: str, stock_id: str, conn: sqlite3.Connection,
                       produced_at: str | None = None, force: bool = False) -> dict | None:
    """把 date 收盤後的個股趨勢訊號轉成次一交易日生效的 target；批數不變（且當天沒發過）、無訊號、商品不可交易皆回 None。"""
    sig = load_stock_trend(stock_id, conn, asof=date)
    if sig is None or sig.date != date:
        logger.warning("build_trend_target: %s %s 無當日趨勢訊號", date, stock_id)
        return None
    instrument = f"TWSE:{stock_id}"
    inst = instrument_get(instrument, conn=conn)
    if not inst or not inst.get("tradable"):
        logger.warning("build_trend_target: %s 不在 instruments 或不可交易，略過", instrument)
        return None
    valid_from = tw_iso(get_next_trading_day(date), settings.TREND_TARGET_VALID_FROM)
    history = _trend_targets(instrument, conn)
    earlier = [t for t in history if t["valid_from"] < valid_from]
    same_day = [t for t in history if t["valid_from"] == valid_from]
    if any(t["valid_from"] > valid_from for t in history):
        logger.warning("build_trend_target: %s 在 %s 之後已有 target；狀態只往前重放，後面日期請依序重建", instrument, valid_from)
    prev = earlier[-1] if earlier else None
    prev_key = (prev or {}).get("inputs_ref", {}).get("key", {})
    prev_units = int(prev_key.get("units", 0))
    last_add = prev_key.get("last_add_date")
    gate_fail = sig.fundamental_gate == GATE_FAIL
    units, why = decide_units(prev_units, sig.big_trend, _effective_action(sig), sig.position_hint,
                              _days_since(last_add, date), gate_fail)
    if units == prev_units and not same_day:
        logger.info("build_trend_target: %s %s %s，不發 target", date, stock_id, why)
        return None
    if units == prev_units:                                          # 重算後變成不變：覆蓋先前發出的那筆為「維持原批數」
        logger.warning("build_trend_target: %s %s 重算後批數不變，覆蓋先前 target 為維持 %d 批", date, stock_id, units)
        why = f"重算後{why}（覆蓋先前發出的 target）"

    if units > prev_units:                                           # 開倉／加倉：重算失效與 Trade Plan
        close = _close_on(date, stock_id, conn)
        invalidation, level = _invalidation(instrument, sig.value_center)
        if close is None or level is None:
            logger.warning("build_trend_target: %s %s 無收盤價或月線（close=%s, level=%s），不開新倉", date, stock_id, close, level)
            return None
        if close <= level:
            logger.warning("build_trend_target: %s %s 收盤 %s 已在失效價位 %s 之下，不開新倉", date, stock_id, close, level)
            return None
        plan = _trade_plan(close, level)
        last_add = date
    elif prev is not None:                                           # 減倉／出場／維持：Trade Plan 與失效沿用上一筆
        invalidation, plan = prev["invalidation"], prev["trade_plan"]
    else:                                                            # 沒有上一筆卻要覆蓋同日 target（重算後不進場）
        invalidation, plan = same_day[-1]["invalidation"], same_day[-1]["trade_plan"]
    rv = f"{sig.rule_version or settings.TREND_RULE_VERSION}-{settings.BACKTEST_RULE_VERSION}"
    record = {
        "schema_version": schema_version("targets"),
        "target_id": target_id_for(STRATEGY_ID, instrument, valid_from, rv),
        "strategy_id": STRATEGY_ID,
        "rule_version": rv,
        "instrument_id": instrument,
        "target_weight": cumulative_weight(units),
        "target_quantity": None,
        "conviction": None,
        "horizon": "swing",
        "produced_at": produced_at or now_tw_iso(),
        "valid_from": valid_from,
        "valid_until": None,
        "locked_at": None,
        "invalidation": invalidation,
        "trade_plan": plan,
        "reason": why[:200],
        "inputs_ref": {"source_table": "stock_trend_signals",
                       "key": {"date": date, "stock_id": stock_id, "units": units, "last_add_date": last_add},
                       "inputs_hash": None},
    }
    try:
        target_put(conn, record, force=force)
    except TargetLockedError as e:
        logger.warning("build_trend_target: %s；保留既有鎖定內容", e)
        return target_get(conn, record["target_id"])
    logger.info("target %s: %s %s units %d→%d weight=%s", record["target_id"], date, instrument, prev_units, units,
                record["target_weight"])
    return target_get(conn, record["target_id"])                     # 回存檔內容（重算不解除鎖定）


def build_trend_targets(date: str, conn: sqlite3.Connection, force: bool = False) -> list[dict]:
    """對 watchlist 每檔跑 build_trend_target；一檔失敗不影響其他檔。回傳實際發出的 targets。"""
    out: list[dict] = []
    for w in watchlist_list(conn=conn):
        try:
            rec = build_trend_target(date, w["stock_id"], conn, force=force)
        except Exception as e:
            logger.error("build_trend_targets %s %s failed: %s", date, w["stock_id"], e)
            continue
        if rec:
            out.append(rec)
    return out
