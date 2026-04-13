"""匯率衍生指標計算：delta、direction、亞幣同步。"""

import json
import logging
import sqlite3
from datetime import datetime

from config import settings

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
    # Read raw FX data
    fx_data = {}
    for pair in _PAIRS:
        row = conn.execute(
            "SELECT close_16, quote_0845 FROM raw_fx "
            "WHERE date = ? AND currency_pair = ?",
            (date, pair),
        ).fetchone()
        if row:
            fx_data[pair] = {"close_16": row[0], "quote_0845": row[1]}

    if not fx_data:
        logger.warning("compute_fx_metrics: no FX data for %s", date)
        return None

    # Compute deltas
    deltas = {}
    for pair in _PAIRS:
        d = fx_data.get(pair)
        if d and d["close_16"] is not None and d["quote_0845"] is not None:
            deltas[pair] = round(d["quote_0845"] - d["close_16"], 6)
        else:
            deltas[pair] = None

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
