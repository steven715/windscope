"""收盤驗證 job（13:40）：收集當日加權指數 OHLC，驗證早上的訊號。"""

import logging
import sqlite3

from db.connection import get_connection
from jobs.helpers import determine_status, run_step
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_verify_close(date: str, db_path: str | None = None) -> dict:
    """
    收盤驗證 job。收集加權指數 OHLC，比對當日訊號與實際走勢。

    回傳格式同其他 jobs，額外包含 verification 結果 dict。
    """
    logger.info("run_verify_close: starting for %s", date)

    if not is_trading_day(date):
        logger.info("run_verify_close: %s is not a trading day, skipping", date)
        return {
            "date": date, "status": "skipped",
            "results": {}, "errors": [], "verification": None,
        }

    results = {}
    errors = []
    verification = None

    # 1. 收集當日加權指數 OHLC（會寫入 raw_index）
    ok, err = run_step("index_ohlc", lambda: _collect_index_ohlc(date, db_path))
    results["index_ohlc"] = ok
    if err:
        errors.append(err)

    # 2. 驗證訊號
    with get_connection(db_path) as conn:
        ok, err = run_step("verify", lambda: _verify(date, conn))
        if ok:
            from integration.verification import verify_signal
            verification = verify_signal(date, conn)
        results["verify"] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_verify_close: %s status=%s", date, status)
    return {
        "date": date, "status": status,
        "results": results, "errors": errors,
        "verification": verification,
    }


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _collect_index_ohlc(date: str, db_path: str | None) -> bool:
    """收集加權指數當日 OHLC。"""
    from collectors.twse import TWSECollector

    c = TWSECollector(db_path=db_path)
    data = c.collect_index_ohlc(date)
    if data is None:
        return False
    c.save_index_ohlc(date, data)
    return True


def _verify(date: str, conn: sqlite3.Connection) -> bool:
    """執行訊號驗證。"""
    from integration.verification import verify_signal

    result = verify_signal(date, conn)
    return result is not None
