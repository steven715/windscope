"""籌碼分點收集 job：個股收盤 + 分點進出 + 算籌碼指標，自成一條龍。

自給自足，不依賴 after_close：自己收個股收盤價（寫 raw_chip 的 __PRICE_ONLY__ 列，
供 MA20/價位計算）→ 收分點進出（FinMind，未設 token 則整步略過、不計失敗，可改用
/chip-import 手動匯入）→ 算籌碼衍生指標(daily_stock_metrics)，供隔日個股觀察訊號。

預設停用（JOB_DEFS enabled_default=False）：串好分點來源後再於排程頁開啟。
"""

import logging
import sqlite3

from config import settings
from db.connection import get_connection
from db.watchlist import watchlist_list
from jobs.helpers import JobRun
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_chip_collect(date: str, db_path: str | None = None) -> dict:
    """籌碼分點收集 job。非交易日略過。回傳 {date, status, results, errors}。"""
    if not is_trading_day(date):
        return JobRun.skipped("chip_collect", date, "非交易日")

    from collectors.chip import ChipCollector
    from collectors.twse import TWSECollector

    job = JobRun("chip_collect", date)

    with get_connection(db_path) as conn:
        # 觀察名單為空 → 個股收盤與分點整步略過、不計失敗（沒有股票可收）
        if watchlist_list(conn=conn):
            twse = TWSECollector(db_path=db_path)
            # 1. TWSE: 個股收盤價（watchlist）——寫 __PRICE_ONLY__ 列供 MA20/價位計算
            #    （空清單也視為無資料）
            job.collect("twse_stock_close", lambda: twse.collect_all_stock_close(date) or None,
                        lambda d: twse.save_stock_close(date, d))

            # 2. Chip: 分點進出（FinMind）。未設 FINMIND_TOKEN 時整步跳過、不計為失敗
            #    （分點自動源需付費 token；可改用 /chip-import 手動匯入）。
            if settings.FINMIND_TOKEN:
                chip = ChipCollector(db_path=db_path)
                job.collect("chip", lambda: chip.collect_broker_trading(date),
                            lambda d: chip.save(date, d))
            else:
                logger.info("chip_collect: 未設 FINMIND_TOKEN，分點自動收集略過（可用 /chip-import 手動匯入）")
        else:
            logger.info("chip_collect: 觀察名單為空，個股收盤與分點略過")

        # 3. Integration: compute_chip_metrics → daily_stock_metrics
        job.step("integration_chip", lambda: _compute_chip(date, conn))

    return job.finish()


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _compute_chip(date: str, conn: sqlite3.Connection) -> bool:
    """計算籌碼衍生指標（即使無 chip 資料也算成功，不是錯誤）。"""
    from integration.chip_metrics import compute_chip_metrics

    compute_chip_metrics(date, conn)
    return True
