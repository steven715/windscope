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
from jobs.helpers import determine_status, run_step
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_chip_collect(date: str, db_path: str | None = None) -> dict:
    """籌碼分點收集 job。非交易日略過。回傳 {date, status, results, errors}。"""
    logger.info("run_chip_collect: starting for %s", date)

    if not is_trading_day(date):
        logger.info("run_chip_collect: %s is not a trading day, skipping", date)
        return {"date": date, "status": "skipped", "results": {}, "errors": []}

    results = {}
    errors = []

    with get_connection(db_path) as conn:
        # 1. TWSE: 個股收盤價（watchlist）——寫 __PRICE_ONLY__ 列供 MA20/價位計算
        ok, err = run_step("twse_stock_close", lambda: _collect_twse_stock_close(date, conn))
        results["twse_stock_close"] = ok
        if err:
            errors.append(err)

        # 2. Chip: 分點進出（FinMind）。未設 FINMIND_TOKEN 時整步跳過、不計為失敗
        #    （分點自動源需付費 token；可改用 /chip-import 手動匯入）。
        if settings.FINMIND_TOKEN:
            ok, err = run_step("chip", lambda: _collect_chip(date, conn))
            results["chip"] = ok
            if err:
                errors.append(err)
        else:
            logger.info("chip_collect: 未設 FINMIND_TOKEN，分點自動收集略過（可用 /chip-import 手動匯入）")

        # 3. Integration: compute_chip_metrics → daily_stock_metrics
        ok, err = run_step("integration_chip", lambda: _compute_chip(date, conn))
        results["integration_chip"] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_chip_collect: %s status=%s", date, status)
    return {"date": date, "status": status, "results": results, "errors": errors}


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _collect_twse_stock_close(date: str, conn: sqlite3.Connection) -> bool:
    """收集個股收盤價（watchlist）。"""
    from collectors.twse import TWSECollector

    c = TWSECollector()
    data = c.collect_all_stock_close(date)
    if not data:
        return False
    c.save_stock_close(date, data)
    return True


def _collect_chip(date: str, conn: sqlite3.Connection) -> bool:
    """收集分點進出（FinMind 自動來源，未設 token 時回 False 但不報錯）。"""
    from collectors.chip import ChipCollector

    c = ChipCollector()
    data = c.collect_broker_trading(date)
    if data is None:
        return False
    c.save(date, data)
    return True


def _compute_chip(date: str, conn: sqlite3.Connection) -> bool:
    """計算籌碼衍生指標（即使無 chip 資料也算成功，不是錯誤）。"""
    from integration.chip_metrics import compute_chip_metrics

    compute_chip_metrics(date, conn)
    return True
