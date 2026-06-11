"""Layer 4 驗證引擎：收盤後比對早上訊號與實際走勢，記錄命中並統計命中率。

雙基準：主基準 = 當日收盤漲跌（hit_day），輔基準 = 開盤跳空方向（hit_open）。
實際走勢三分類：漲 > +VERIFY_FLAT_BAND_PCT% / 跌 < -門檻 / 平（其間）。
"""

import logging
import sqlite3
from datetime import datetime

from config import settings
from utils.trading_calendar import get_previous_trading_day

logger = logging.getLogger(__name__)

_DIRECTION_TO_CLASS = {"bullish": "up", "bearish": "down", "neutral": "flat"}


def _classify_change(pct: float) -> str:
    """漲跌幅三分類：up / down / flat（|pct| <= 中性帶）。"""
    if pct > settings.VERIFY_FLAT_BAND_PCT:
        return "up"
    elif pct < -settings.VERIFY_FLAT_BAND_PCT:
        return "down"
    else:
        return "flat"


def verify_signal(date: str, conn: sqlite3.Connection) -> dict | None:
    """驗證 date 的訊號：比對 raw_index 實際走勢，寫入 verifications。

    需要：signals 有當日訊號、raw_index 有當日 OHLC 與前一交易日收盤。
    任一缺失回傳 None。
    """
    signal = conn.execute(
        "SELECT direction, confidence FROM signals WHERE date = ?", (date,)
    ).fetchone()
    if signal is None:
        logger.warning("verify_signal: no signal for %s", date)
        return None
    predicted_direction, confidence = signal

    index_row = conn.execute(
        "SELECT open, close FROM raw_index WHERE date = ?", (date,)
    ).fetchone()
    if index_row is None or index_row[0] is None or index_row[1] is None:
        logger.warning("verify_signal: no index OHLC for %s", date)
        return None
    open_price, close_price = index_row

    prev_day = get_previous_trading_day(date, conn)
    prev_row = None
    if prev_day:
        prev_row = conn.execute(
            "SELECT close FROM raw_index WHERE date = ?", (prev_day,)
        ).fetchone()
    if prev_row is None or prev_row[0] is None:
        logger.warning("verify_signal: no previous close for %s (prev=%s)",
                       date, prev_day)
        return None
    prev_close = prev_row[0]

    open_gap_pct = round((open_price - prev_close) / prev_close * 100, 4)
    day_change_pct = round((close_price - prev_close) / prev_close * 100, 4)

    open_gap_class = _classify_change(open_gap_pct)
    day_change_class = _classify_change(day_change_pct)
    predicted_class = _DIRECTION_TO_CLASS.get(predicted_direction)

    hit_day = 1 if predicted_class == day_change_class else 0
    hit_open = 1 if predicted_class == open_gap_class else 0

    result = {
        "date": date,
        "predicted_direction": predicted_direction,
        "confidence": confidence,
        "prev_close": prev_close,
        "open": open_price,
        "close": close_price,
        "open_gap_pct": open_gap_pct,
        "day_change_pct": day_change_pct,
        "open_gap_class": open_gap_class,
        "day_change_class": day_change_class,
        "hit_day": hit_day,
        "hit_open": hit_open,
    }

    conn.execute(
        """INSERT INTO verifications
               (date, predicted_direction, confidence, prev_close,
                open, close, open_gap_pct, day_change_pct,
                open_gap_class, day_change_class, hit_day, hit_open,
                verified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               predicted_direction = excluded.predicted_direction,
               confidence = excluded.confidence,
               prev_close = excluded.prev_close,
               open = excluded.open,
               close = excluded.close,
               open_gap_pct = excluded.open_gap_pct,
               day_change_pct = excluded.day_change_pct,
               open_gap_class = excluded.open_gap_class,
               day_change_class = excluded.day_change_class,
               hit_day = excluded.hit_day,
               hit_open = excluded.hit_open,
               verified_at = excluded.verified_at""",
        (date, predicted_direction, confidence, prev_close,
         open_price, close_price, open_gap_pct, day_change_pct,
         open_gap_class, day_change_class, hit_day, hit_open,
         datetime.now().isoformat()),
    )
    conn.commit()

    logger.info("Verified %s: predicted=%s actual_day=%s hit_day=%d hit_open=%d",
                date, predicted_direction, day_change_class, hit_day, hit_open)
    return result


def get_verification_stats(conn: sqlite3.Connection, last_n: int = 20) -> dict:
    """統計最近 last_n 筆驗證的命中率（主/輔基準）與各信心度命中率。"""
    rows = conn.execute(
        "SELECT confidence, hit_day, hit_open FROM verifications "
        "ORDER BY date DESC LIMIT ?",
        (last_n,),
    ).fetchall()

    total = len(rows)
    if total == 0:
        return {
            "total": 0, "hit_day_rate": None, "hit_open_rate": None,
            "by_confidence": {},
        }

    hit_day_count = sum(r[1] for r in rows)
    hit_open_count = sum(r[2] for r in rows)

    by_confidence: dict[int, dict] = {}
    for confidence, hit_day, _hit_open in rows:
        bucket = by_confidence.setdefault(confidence, {"total": 0, "hits": 0})
        bucket["total"] += 1
        bucket["hits"] += hit_day
    for bucket in by_confidence.values():
        bucket["rate"] = round(bucket["hits"] / bucket["total"] * 100, 1)

    return {
        "total": total,
        "hit_day_rate": round(hit_day_count / total * 100, 1),
        "hit_open_rate": round(hit_open_count / total * 100, 1),
        "by_confidence": by_confidence,
    }


def get_recent_verifications(conn: sqlite3.Connection, last_n: int = 20) -> list[dict]:
    """回傳最近 last_n 筆驗證紀錄（新到舊），供頁面與查詢使用。"""
    rows = conn.execute(
        "SELECT date, predicted_direction, confidence, day_change_pct, "
        "       day_change_class, open_gap_pct, open_gap_class, "
        "       hit_day, hit_open "
        "FROM verifications ORDER BY date DESC LIMIT ?",
        (last_n,),
    ).fetchall()
    return [
        {
            "date": r[0], "predicted_direction": r[1], "confidence": r[2],
            "day_change_pct": r[3], "day_change_class": r[4],
            "open_gap_pct": r[5], "open_gap_class": r[6],
            "hit_day": r[7], "hit_open": r[8],
        }
        for r in rows
    ]
