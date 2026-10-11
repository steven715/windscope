"""收盤後 job：收集當天收盤後的「市場面」資料，供隔日基準。

含：加權指數收盤(spot_close，隔日 after_night 算期貨缺口的基準)、三大法人、外資個股、
除息預估點數、外資期貨未平倉。
拆出去的部分：個股收盤＋分點＋籌碼指標 → chip_collect job；FX 16:00 收盤 → afternoon_fx(16:00)。
"""

import logging
import sqlite3

from db.connection import get_connection
from db.watchlist import watchlist_list
from jobs.helpers import JobRun
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_after_close(date: str, db_path: str | None = None) -> dict:
    """
    收盤後 job。收集收盤後的市場面資料（指數收盤／三大法人／外資個股／除息／期貨未平倉）。

    回傳：
    {
        "date": "2026-04-08",
        "status": "completed" | "partial" | "skipped" | "failed",
        "results": {...},
        "errors": [...]
    }
    """
    if not is_trading_day(date):
        return JobRun.skipped("after_close", date, "非交易日")

    from collectors.taifex import TAIFEXCollector
    from collectors.twse import TWSECollector

    job = JobRun("after_close", date)
    twse = TWSECollector(db_path=db_path)
    taifex = TAIFEXCollector(db_path=db_path)

    with get_connection(db_path) as conn:
        # 1. TWSE: 加權指數收盤價（spot_close）——隔日 after_night 算期貨夜盤缺口的基準
        job.collect("twse_spot_close", lambda: twse.collect_spot_close(date),
                    lambda d: twse.save_spot_close(date, d))
        # 2. TWSE: 三大法人買賣超
        job.collect("twse_institutional", lambda: twse.collect_institutional(date),
                    lambda d: twse.save_institutional(date, d))
        # 3. TWSE: 外資個股買賣超（watchlist）
        job.collect("twse_foreign_stock", lambda: twse.collect_foreign_stock(date),
                    lambda d: twse.save_foreign_stock(date, d))
        # 4. TWSE: 除息預估點數
        job.collect("twse_ex_dividend", lambda: twse.collect_ex_dividend_points(date),
                    lambda d: twse.save_ex_dividend(date, d))
        # 5. TAIFEX: 外資期貨未平倉
        job.collect("taifex_oi", lambda: taifex.collect_oi_foreign(date),
                    lambda d: taifex.save_oi_foreign(date, d))

        # 6. 個股日K + 趨勢訊號：更新 watchlist 當月日K並重算趨勢（spec v2 趨勢層）
        #    觀察名單為空 → 整步略過、不計失敗
        if watchlist_list(conn=conn):
            job.step("stock_trends", lambda: _refresh_stock_trends(date, conn, db_path))
        else:
            logger.info("after_close: 觀察名單為空，個股日K與趨勢略過")

    return job.finish()


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _refresh_stock_trends(date: str, conn: sqlite3.Connection,
                          db_path: str | None) -> bool:
    """更新 watchlist 個股當月日K並重算趨勢訊號。至少一檔成功回 True，無名單回 False。"""
    from collectors.twse import TWSECollector
    from integration.trend_signal import compute_trend_signal

    watch = [w["stock_id"] for w in watchlist_list(conn=conn)]
    if not watch:
        return False

    collector = TWSECollector(db_path=db_path)
    any_ok = False
    for stock_id in watch:
        try:
            bars = collector.collect_stock_ohlc_month(date, stock_id)
            if bars:
                collector.save_stock_ohlc(stock_id, bars)
            # 只有真的算出訊號才算成功；剛加入尚未回補歷史的股會回 None，不虛報成功
            if compute_trend_signal(stock_id, conn) is not None:
                any_ok = True
        except Exception as e:
            logger.error("_refresh_stock_trends %s failed: %s", stock_id, e)
    return any_ok
