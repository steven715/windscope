"""純導出層：給定 date，從原始事實（raw_*）重新導出 L2 衍生指標與 L3 訊號。

functional core：recompute_date(date) = f(raw, config)，同一輸入恆得同一輸出、
與「何時執行」無關。本模組不收集任何外部資料，只讀 raw_* 重算下列衍生資料
（皆以 ON CONFLICT/REPLACE 冪等覆寫）：
  L2  daily_metrics（fx / futures）、daily_stock_metrics（chip）
  L3  stock_signals（個股觀察）、signals（市場訊號）

順序固定：先 L2 指標、再 L3 訊號（市場訊號讀 daily_metrics、個股訊號讀籌碼指標）。
"""

import logging
import sqlite3

from integration.chip_metrics import compute_chip_metrics
from integration.futures_metrics import compute_futures_metrics
from integration.fx_metrics import compute_fx_metrics
from integration.signal_engine import compute_market_signal, compute_stock_signals

logger = logging.getLogger(__name__)


def recompute_date(date: str, conn: sqlite3.Connection) -> dict:
    """從 raw 重算 date 的衍生指標與訊號，覆寫既有衍生資料。回傳各步驟摘要。"""
    fx = compute_fx_metrics(date, conn)
    futures = compute_futures_metrics(date, conn)
    chip = compute_chip_metrics(date, conn)
    stock_signals = compute_stock_signals(date, conn)
    signal = compute_market_signal(date, conn)

    logger.info(
        "recompute_date %s: fx=%s futures=%s chip=%d stock_signals=%d signal=%s",
        date, fx is not None, futures is not None,
        len(chip) if chip else 0, len(stock_signals) if stock_signals else 0,
        signal["direction"] if signal else None,
    )
    return {
        "date": date,
        "fx_metrics": fx is not None,
        "futures_metrics": futures is not None,
        "chip_metrics": len(chip) if chip else 0,
        "stock_signals": len(stock_signals) if stock_signals else 0,
        "signal": signal,
    }
