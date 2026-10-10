"""期貨衍生指標計算：spread、均量比、未平倉。"""

import logging
import sqlite3
from datetime import datetime

from integration.premarket_inputs import load_premarket_inputs

logger = logging.getLogger(__name__)


def compute_futures_metrics(date: str, conn: sqlite3.Connection) -> dict | None:
    """計算期貨衍生指標，寫入 daily_metrics。回傳結果 dict 或 None（無原始資料）。"""
    inp = load_premarket_inputs(date, conn)
    if not inp.has_futures_row:
        logger.warning("compute_futures_metrics: no futures data for %s", date)
        return None

    # 1. futures_spread = 今日夜盤收盤 − 前一交易日現貨收盤（隔夜期貨缺口）。
    #    基準（含 raw_index fallback）由盤前輸入決定，與解讀／摘要同一份。
    if inp.night_close is not None and inp.prev_spot is not None:
        futures_spread = round(inp.night_close - inp.prev_spot, 2)
    else:
        futures_spread = None

    # 2. futures_spread_adjusted
    if futures_spread is not None:
        if inp.ex_dividend_points is not None:
            futures_spread_adjusted = round(futures_spread - inp.ex_dividend_points, 2)
        else:
            futures_spread_adjusted = futures_spread
    else:
        futures_spread_adjusted = None

    # 3. futures_volume_ratio
    futures_volume_ratio = None
    avg_volume = inp.night_volume_avg
    if inp.night_volume is not None and avg_volume:
        futures_volume_ratio = round(inp.night_volume / avg_volume, 2)

    # 4/5. 外資未平倉：訊號於 08:50 產出時，今日 OI 尚未收（18:30 才收），故採
    #      「前一交易日收盤」的外資未平倉作為訊號時點已知的最新部位；oi_delta 為
    #      其相對「再前一交易日」的增減（最近一場的部位變化，供摘要顯示）。
    oi_net_foreign = inp.oi_prev
    oi_delta = None
    if inp.oi_prev is not None and inp.oi_prev2 is not None:
        oi_delta = inp.oi_prev - inp.oi_prev2

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
