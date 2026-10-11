"""純導出層：給定 date，從原始事實（raw_*）重新導出 L2 衍生指標與 L3 盤前判斷／籌碼觀察。

functional core：recompute_date(date) = f(raw, config)，同一輸入恆得同一輸出、
與「何時執行」無關。本模組不收集任何外部資料，只讀 raw_* 重算下列衍生資料
（皆以 ON CONFLICT/REPLACE 冪等覆寫）：
  L2  daily_metrics（fx / futures）、daily_stock_metrics（chip）
  L3  stock_signals（籌碼觀察）、signals（盤前判斷）

順序固定：先 L2 指標、再 L3（盤前判斷讀 daily_metrics、籌碼觀察讀籌碼指標）。

注意：第 T 日的籌碼觀察讀「T 之前」的分點指標，單日重算不會一併重算前一日的
分點指標；要一致請由舊到新重算整段區間。排程 job 不走本模組（見 ADR-0003）。
"""

import logging
import sqlite3

from integration.chip_metrics import compute_chip_metrics
from integration.futures_metrics import compute_futures_metrics
from integration.fx_metrics import compute_fx_metrics
from integration.premarket_call import compute_premarket_call
from integration.chip_observation import refresh_chip_observations

logger = logging.getLogger(__name__)


def recompute_date(date: str, conn: sqlite3.Connection) -> dict:
    """從 raw 重算 date 的衍生指標與訊號，覆寫既有衍生資料。回傳各步驟摘要。"""
    fx = compute_fx_metrics(date, conn)
    futures = compute_futures_metrics(date, conn)
    chip = compute_chip_metrics(date, conn)
    observations = refresh_chip_observations(date, conn)
    signal = compute_premarket_call(date, conn)

    logger.info(
        "recompute_date %s: fx=%s futures=%s chip=%d observations=%d signal=%s",
        date, fx is not None, futures is not None,
        len(chip) if chip else 0, len(observations),
        signal["direction"] if signal else None,
    )
    return {
        "date": date,
        "fx_metrics": fx is not None,
        "futures_metrics": futures is not None,
        "chip_metrics": len(chip) if chip else 0,
        "chip_observations": len(observations),
        "signal": signal,
    }
