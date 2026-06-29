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


def offshore_twd_morning(conn: sqlite3.Connection, date: str) -> float | None:
    """取某日離岸 USD/TWD 晨間最後一根 5 分 K（≈08:50 報價）的收盤，無則 None。

    來源 intraday_fx（before_open 收的 Yahoo USDTWD=X 離岸序列）。在岸台銀 08:45
    牌價開盤前未更新＝前一日收盤，隔夜 delta 失真，故 TWD 隔夜變動改用此離岸值。
    """
    row = conn.execute(
        "SELECT close FROM intraday_fx "
        "WHERE date = ? AND currency_pair = 'USD/TWD' "
        "ORDER BY ts DESC LIMIT 1",
        (date,),
    ).fetchone()
    return row[0] if row else None


def compute_fx_metrics(date: str, conn: sqlite3.Connection) -> dict | None:
    """計算匯率衍生指標，寫入 daily_metrics。回傳結果 dict 或 None（無原始資料）。"""
    # CNY/KRW delta = 當日 08:45 報價 − 前一交易日 16:00 收盤（Yahoo 真實報價，隔夜有效）。
    # TWD delta = 離岸晨對晨（今晨 − 昨晨離岸，取自 intraday_fx）；在岸 08:45 牌價開盤前
    #   ＝前一日收盤、隔夜變動≈0 失真，故 TWD 不走在岸 quote_0845/close_16。
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

    # Compute deltas：CNY/KRW 走在岸 08:45 − 前日 16:00；TWD 走離岸晨對晨。
    deltas = {}
    for pair in ("USD/CNY", "USD/KRW"):
        quote = today_quotes.get(pair)
        prev_close = prev_closes[pair]
        if quote is not None and prev_close is not None:
            deltas[pair] = round(quote - prev_close, 6)
        else:
            deltas[pair] = None

    twd_today = offshore_twd_morning(conn, date)
    twd_prev = offshore_twd_morning(conn, prev_day) if prev_day else None
    if twd_today is not None and twd_prev is not None:
        deltas["USD/TWD"] = round(twd_today - twd_prev, 6)
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
