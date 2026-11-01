"""還原股價：windscope 端的 DB 讀取薄層（純函式在共用庫 strategylib.adjust）。

付費還原源（FinMind TaiwanStockPriceAdj）需 Sponsor，故自行還原：分割/大除權由價格缺口偵測、
配息用免費 TaiwanStockDividendResult 的 after/before 比例（見 strategylib.adjust）。
load_adjusted_bars 是「趨勢層眼中的個股價格序列」唯一讀取入口（趨勢訊號、回測、K線圖、
OHLC API 共用）；load_bars 為其原始價底層。
"""

import logging
import sqlite3

from strategylib.adjust import (  # noqa: F401  純函式在共用庫；這裡重新匯出＋DB 讀取
    DEFAULT_GAP_PCT,
    adjust_note,
    back_adjust,
    back_adjust_with_events,
)

logger = logging.getLogger(__name__)


def load_bars(stock_id: str, conn: sqlite3.Connection,
              asof: str | None = None) -> list[dict]:
    """讀 raw_stock_daily 原始成交價（排除 OHLC 缺值列），舊→新。

    bars = [{date, open, high, low, close, volume}]；asof 給定只取該日（含）以前。
    """
    q = ("SELECT date, open, high, low, close, volume "
         "FROM raw_stock_daily WHERE stock_id = ? AND open IS NOT NULL "
         "AND high IS NOT NULL AND low IS NOT NULL AND close IS NOT NULL")
    args: list = [stock_id]
    if asof:
        q += " AND date <= ?"
        args.append(asof)
    rows = conn.execute(q + " ORDER BY date ASC", args).fetchall()
    return [{"date": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5]} for r in rows]


def load_adjusted_bars(stock_id: str, conn: sqlite3.Connection,
                       asof: str | None = None) -> tuple[list[dict], list[dict]]:
    """讀 K 線並還原，回傳 (bars 舊→新, 還原事件)。asof 給定只取該日以前（回測不看未來）。"""
    from collectors.dividend import load_dividend_factors

    bars = load_bars(stock_id, conn, asof)
    if not bars:
        return [], []
    return back_adjust_with_events(bars, load_dividend_factors(conn, stock_id))
