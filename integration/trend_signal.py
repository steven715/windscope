"""Layer 3b 個股趨勢訊號：windscope 端的 DB 存取薄層（規則本體在共用庫 strategylib.trend）。

evaluate／evaluate_series／TrendParams／常數由 strategylib.trend 重新匯出，既有 import 不變；
本檔只保留讀還原K線→算→寫 stock_trend_signals、以及 TrendReading 讀取入口。
門檻的單一真相在 strategylib.trend.DEFAULTS（settings 的 TREND_* 由它取值）；
改門檻 bump strategylib.trend.RULE_VERSION。
"""

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from config import settings
from strategylib.trend import (  # noqa: F401  純函式與常數在共用庫；這裡只重新匯出＋DB 存取
    DOWN,
    FLAT,
    GATE_FAIL,
    GATE_PASS,
    GATE_UNKNOWN,
    RULE_VERSION,
    TREND_CSS,
    TREND_LABELS,
    UNKNOWN,
    UP,
    TrendParams,
    _hysteresis,
    _position,
    _route,
    evaluate,
    evaluate_series,
    sma,
)

logger = logging.getLogger(__name__)


def _save_trend_signal(conn: sqlite3.Connection, result: dict) -> None:
    """把 evaluate 結果 upsert 進 stock_trend_signals。"""
    conn.execute(
        """INSERT INTO stock_trend_signals
               (date, stock_id, big_trend, big_trend_confidence, value_center,
                bias_pct, small_state, small_turn_up, fundamental_gate,
                action_hint, position_hint, flat_redirect, reasons,
                adjust_note, rule_version, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date, stock_id) DO UPDATE SET
               big_trend = excluded.big_trend,
               big_trend_confidence = excluded.big_trend_confidence,
               value_center = excluded.value_center,
               bias_pct = excluded.bias_pct,
               small_state = excluded.small_state,
               small_turn_up = excluded.small_turn_up,
               fundamental_gate = excluded.fundamental_gate,
               action_hint = excluded.action_hint,
               position_hint = excluded.position_hint,
               flat_redirect = excluded.flat_redirect,
               reasons = excluded.reasons,
               adjust_note = excluded.adjust_note,
               rule_version = excluded.rule_version,
               created_at = excluded.created_at""",
        (result["date"], result["stock_id"], result["big_trend"],
         result["big_trend_confidence"], result["value_center"],
         result["bias_pct"], result["small_state"],
         1 if result["small_turn_up"] else 0, result["fundamental_gate"],
         result["action_hint"], result["position_hint"], result["flat_redirect"],
         json.dumps(result["reasons"], ensure_ascii=False),
         result.get("adjust_note"),
         settings.TREND_RULE_VERSION, datetime.now().isoformat()),
    )
    conn.commit()


@dataclass(frozen=True)
class TrendReading:
    """stock_trend_signals 的一筆趨勢訊號（reasons 已解開）；label／css 為大勢顯示用。"""

    date: str
    big_trend: str | None
    big_trend_confidence: float | None
    value_center: float | None
    bias_pct: float | None
    small_state: str | None
    small_turn_up: bool
    fundamental_gate: str | None
    action_hint: str | None
    position_hint: str | None
    flat_redirect: str | None
    reasons: list
    adjust_note: str | None
    rule_version: str | None

    @property
    def label(self) -> str | None:
        """大勢中文標籤（例：上升趨勢）。"""
        return TREND_LABELS.get(self.big_trend, self.big_trend)

    @property
    def css(self) -> str:
        """大勢配色 class。"""
        return TREND_CSS.get(self.big_trend, "flat")


_READING_COLUMNS = (
    "date", "big_trend", "big_trend_confidence", "value_center", "bias_pct",
    "small_state", "small_turn_up", "fundamental_gate", "action_hint",
    "position_hint", "flat_redirect", "reasons", "adjust_note", "rule_version",
)


def load_stock_trend(stock_id: str, conn: sqlite3.Connection,
                     asof: str | None = None) -> TrendReading | None:
    """讀個股最新一筆趨勢訊號（唯一讀取入口）；asof 給定時取該日（含）以前最新。無則 None。"""
    where, params = "stock_id = ?", [stock_id]
    if asof is not None:
        where += " AND date <= ?"
        params.append(asof)
    row = conn.execute(
        f"SELECT {', '.join(_READING_COLUMNS)} FROM stock_trend_signals "
        f"WHERE {where} ORDER BY date DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    values = dict(zip(_READING_COLUMNS, row))
    values["small_turn_up"] = bool(values["small_turn_up"])
    values["reasons"] = json.loads(values["reasons"]) if values["reasons"] else []
    return TrendReading(**values)


def compute_trend_signal(stock_id: str, conn: sqlite3.Connection,
                         asof: str | None = None,
                         params: TrendParams | None = None) -> dict | None:
    """讀 raw_stock_daily 算個股趨勢訊號並寫入 stock_trend_signals。無K線回 None。

    asof 給定時只取該日（含）以前的K線（供回測/歷史重算）；否則用全部歷史的最新一根。
    """
    # 還原股價：偵測分割/大除權缺口 + 配息比例回推（spec §6.1 需還原股價）
    from integration.price_adjust import adjust_note, load_adjusted_bars
    bars, adj_events = load_adjusted_bars(stock_id, conn, asof)
    if not bars:
        logger.info("compute_trend_signal: no OHLC for %s", stock_id)
        return None
    note = adjust_note(adj_events)

    result = evaluate(bars, params)
    result["stock_id"] = stock_id
    result["date"] = bars[-1]["date"]
    result["rule_version"] = settings.TREND_RULE_VERSION
    result["adjust_note"] = note
    _save_trend_signal(conn, result)

    # 公司行為缺口還原可能誤判（±10% 限制外的槓桿/反向ETF、停牌跳空）→ 記 log 供核對
    for e in adj_events:
        if e["kind"] == "gap":
            logger.info("Trend %s: back-adjusted gap @%s ×%.3f (verify split/ex-rights)",
                        stock_id, e["date"], e["factor"])

    logger.info("Trend signal for %s@%s: %s / %s (bias=%s)",
                stock_id, result["date"], result["big_trend"],
                result["action_hint"], result["bias_pct"])
    return result
