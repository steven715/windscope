"""期貨衍生指標計算：spread、均量比、未平倉。"""

import logging
import sqlite3
from datetime import datetime

from config import settings
from utils.trading_calendar import get_previous_trading_day

logger = logging.getLogger(__name__)


def compute_futures_metrics(date: str, conn: sqlite3.Connection) -> dict | None:
    """計算期貨衍生指標，寫入 daily_metrics。回傳結果 dict 或 None（無原始資料）。"""
    row = conn.execute(
        "SELECT night_close, night_volume, spot_close, "
        "       ex_dividend_points, oi_net_foreign "
        "FROM raw_futures WHERE date = ?",
        (date,),
    ).fetchone()

    if row is None:
        logger.warning("compute_futures_metrics: no futures data for %s", date)
        return None

    night_close, night_volume, spot_close, ex_div, oi_net_foreign = row

    # 1. futures_spread
    if night_close is not None and spot_close is not None:
        futures_spread = round(night_close - spot_close, 2)
    else:
        futures_spread = None

    # 2. futures_spread_adjusted
    if futures_spread is not None:
        if ex_div is not None:
            futures_spread_adjusted = round(futures_spread - ex_div, 2)
        else:
            futures_spread_adjusted = futures_spread
    else:
        futures_spread_adjusted = None

    # 3. futures_volume_ratio
    futures_volume_ratio = None
    if night_volume is not None:
        recent = conn.execute(
            "SELECT night_volume FROM raw_futures "
            "WHERE date < ? AND night_volume IS NOT NULL "
            "ORDER BY date DESC LIMIT ?",
            (date, settings.FUTURES_VOLUME_LOOKBACK),
        ).fetchall()
        if recent:
            avg_volume = sum(r[0] for r in recent) / len(recent)
            if avg_volume > 0:
                futures_volume_ratio = round(night_volume / avg_volume, 2)

    # 4. oi_net_foreign (passthrough from raw)
    # 5. oi_delta
    oi_delta = None
    if oi_net_foreign is not None:
        prev_date = get_previous_trading_day(date, conn)
        if prev_date:
            prev_row = conn.execute(
                "SELECT oi_net_foreign FROM raw_futures WHERE date = ?",
                (prev_date,),
            ).fetchone()
            if prev_row and prev_row[0] is not None:
                oi_delta = oi_net_foreign - prev_row[0]

    result = {
        "futures_spread": futures_spread,
        "futures_spread_adjusted": futures_spread_adjusted,
        "futures_volume_ratio": futures_volume_ratio,
        "oi_net_foreign": oi_net_foreign,
        "oi_delta": oi_delta,
    }

    # Write to daily_metrics (only futures columns)
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO daily_metrics
               (date, futures_spread, futures_spread_adjusted,
                futures_volume_ratio, oi_net_foreign, oi_delta, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(date) DO UPDATE SET
               futures_spread = excluded.futures_spread,
               futures_spread_adjusted = excluded.futures_spread_adjusted,
               futures_volume_ratio = excluded.futures_volume_ratio,
               oi_net_foreign = excluded.oi_net_foreign,
               oi_delta = excluded.oi_delta,
               updated_at = excluded.updated_at""",
        (date, futures_spread, futures_spread_adjusted,
         futures_volume_ratio, oi_net_foreign, oi_delta, now),
    )
    conn.commit()

    logger.info("Futures metrics computed for %s: spread=%s, ratio=%s",
                date, futures_spread, futures_volume_ratio)
    return result
