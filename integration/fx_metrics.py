"""匯率衍生指標計算：delta、direction、亞幣同步。"""

import json
import logging
import sqlite3
from datetime import datetime

from config import settings
from integration.premarket_inputs import load_premarket_inputs

logger = logging.getLogger(__name__)

_PAIRS = ["USD/TWD", "USD/CNY", "USD/KRW"]
_THRESHOLDS = {
    "USD/TWD": settings.FX_THRESHOLD_TWD,
    "USD/CNY": settings.FX_THRESHOLD_CNY,
    "USD/KRW": settings.FX_THRESHOLD_KRW,
}
_LABELS = {"USD/TWD": "TWD", "USD/CNY": "CNY", "USD/KRW": "KRW"}


def _classify_direction(delta: float | None, threshold: float) -> str | None:
    """根據 delta 和門檻分類方向。"""
    if delta is None:
        return None
    if delta < -threshold:
        return "bullish"
    elif delta > threshold:
        return "bearish"
    else:
        return "neutral"


def compute_fx_metrics(date: str, conn: sqlite3.Connection) -> dict | None:
    """計算匯率衍生指標，寫入 daily_metrics。回傳結果 dict 或 None（無原始資料）。"""
    # CNY/KRW delta = 當日 08:45 報價 − 前一交易日 16:00 收盤（Yahoo 真實報價，隔夜有效）。
    # TWD delta = 離岸晨對晨（今晨 − 昨晨離岸，取自 intraday_fx）；在岸 08:45 牌價開盤前
    #   ＝前一日收盤、隔夜變動≈0 失真，故 TWD 不走在岸 quote_0845/close_16。
    inp = load_premarket_inputs(date, conn)

    # 當日有 raw_fx row（即使值為 NULL）就視為有資料
    if not any(pair in inp.fx_quote_0845 for pair in _PAIRS):
        logger.warning("compute_fx_metrics: no FX data for %s", date)
        return None

    # Compute deltas：CNY/KRW 走在岸 08:45 − 前日 16:00；TWD 走離岸晨對晨。
    deltas = {}
    for pair in ("USD/CNY", "USD/KRW"):
        quote = inp.fx_quote_0845.get(pair)
        prev_close = inp.fx_prev_close_16.get(pair)
        if quote is not None and prev_close is not None:
            deltas[pair] = round(quote - prev_close, 6)
        else:
            deltas[pair] = None

    if inp.twd_offshore_today is not None and inp.twd_offshore_prev is not None:
        deltas["USD/TWD"] = round(inp.twd_offshore_today - inp.twd_offshore_prev, 6)
    else:
        deltas["USD/TWD"] = None

    fx_delta_twd = deltas["USD/TWD"]
    fx_delta_cny = deltas["USD/CNY"]
    fx_delta_krw = deltas["USD/KRW"]

    # Direction (based on TWD only)
    fx_direction = _classify_direction(fx_delta_twd, settings.FX_THRESHOLD_TWD)

    # Asia sync
    pair_directions = {}
    for pair in _PAIRS:
        pair_directions[pair] = _classify_direction(
            deltas[pair], _THRESHOLDS[pair]
        )

    non_null_dirs = [d for d in pair_directions.values() if d is not None]
    if len(non_null_dirs) == 3 and len(set(non_null_dirs)) == 1:
        fx_asia_sync = 1
    elif len(non_null_dirs) < 2:
        fx_asia_sync = None
    else:
        fx_asia_sync = 0

    # Asia detail JSON
    fx_asia_detail = json.dumps(
        {_LABELS[p]: pair_directions[p] for p in _PAIRS},
        ensure_ascii=False,
    )

    result = {
        "fx_delta_twd": fx_delta_twd,
        "fx_delta_cny": fx_delta_cny,
        "fx_delta_krw": fx_delta_krw,
        "fx_direction": fx_direction,
        "fx_asia_sync": fx_asia_sync,
        "fx_asia_detail": fx_asia_detail,
    }

    # Write to daily_metrics (only FX columns)
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO daily_metrics
               (date, fx_delta_twd, fx_delta_cny, fx_delta_krw,
                fx_direction, fx_asia_sync, fx_asia_detail, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               fx_delta_twd = excluded.fx_delta_twd,
               fx_delta_cny = excluded.fx_delta_cny,
               fx_delta_krw = excluded.fx_delta_krw,
               fx_direction = excluded.fx_direction,
               fx_asia_sync = excluded.fx_asia_sync,
               fx_asia_detail = excluded.fx_asia_detail,
               updated_at = excluded.updated_at""",
        (date, fx_delta_twd, fx_delta_cny, fx_delta_krw,
         fx_direction, fx_asia_sync, fx_asia_detail, now),
    )
    conn.commit()

    logger.info("FX metrics computed for %s: direction=%s, sync=%s",
                date, fx_direction, fx_asia_sync)
    return result
