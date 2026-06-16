"""匯率衍生指標計算：delta、direction、亞幣同步。"""

import json
import logging
import sqlite3
from datetime import datetime

from config import settings
from utils.trading_calendar import get_previous_trading_day

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
    # delta = 當日 08:45 報價 − 前一交易日 16:00 收盤（隔夜匯率變動）。
    # 當日 close_16 要等當天 18:30 才收，故基準必取「前一交易日」的 close_16。
    prev_day = get_previous_trading_day(date, conn)

    # 讀當日報價：只要當日有 row（即使值為 NULL）就視為有資料
    today_quotes = {}
    for pair in _PAIRS:
        row = conn.execute(
            "SELECT quote_0845 FROM raw_fx WHERE date = ? AND currency_pair = ?",
            (date, pair),
        ).fetchone()
        if row:
            today_quotes[pair] = row[0]

    if not today_quotes:
        logger.warning("compute_fx_metrics: no FX data for %s", date)
        return None

    # 讀前一交易日收盤基準
    prev_closes = {}
    for pair in _PAIRS:
        prev_closes[pair] = None
        if prev_day:
            prev_row = conn.execute(
                "SELECT close_16 FROM raw_fx WHERE date = ? AND currency_pair = ?",
                (prev_day, pair),
            ).fetchone()
            if prev_row:
                prev_closes[pair] = prev_row[0]

    # Compute deltas
    deltas = {}
    for pair in _PAIRS:
        quote = today_quotes.get(pair)
        prev_close = prev_closes[pair]
        if quote is not None and prev_close is not None:
            deltas[pair] = round(quote - prev_close, 6)
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
