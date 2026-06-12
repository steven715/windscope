"""夜盤後 job：收集夜盤資料，計算期貨衍生指標。"""

import logging
import sqlite3

from db.connection import get_connection
from jobs.helpers import determine_status, run_step
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_after_night(date: str, db_path: str | None = None) -> dict:
    """
    夜盤後 job。收集夜盤資料，計算期貨衍生指標。

    日期語意：夜盤在凌晨 5:00 收盤，歸屬為當日交易日。
    例如週一晚上開盤、週二凌晨 5:00 收盤，date 應傳入週二。

    回傳格式同 after_close。
    """
    logger.info("run_after_night: starting for %s", date)

    if not is_trading_day(date):
        logger.info("run_after_night: %s is not a trading day, skipping", date)
        return {"date": date, "status": "skipped", "results": {}, "errors": []}

    results = {}
    errors = []

    with get_connection(db_path) as conn:
        # 1. TAIFEX: 夜盤收盤價 + 成交量
        ok, err = run_step("taifex_night", lambda: _collect_taifex_night(date, conn))
        results["taifex_night"] = ok
        if err:
            errors.append(err)

        # 2. FX (Yahoo Finance): S&P 500 收盤 → raw_futures.sp500_close
        ok, err = run_step("sp500_close", lambda: _collect_sp500(date, conn))
        results["sp500_close"] = ok
        if err:
            errors.append(err)

        # 3. Integration: compute_futures_metrics
        ok, err = run_step("integration_futures", lambda: _compute_futures(date, conn))
        results["integration_futures"] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_after_night: %s status=%s", date, status)
    return {"date": date, "status": status, "results": results, "errors": errors}


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _collect_taifex_night(date: str, conn: sqlite3.Connection) -> bool:
    """收集夜盤收盤價和成交量。"""
    from collectors.taifex import TAIFEXCollector

    c = TAIFEXCollector()
    data = c.collect_night_session(date)
    if data is None:
        return False
    c.save_night_session(date, data)
    return True


def _collect_sp500(date: str, conn: sqlite3.Connection) -> bool:
    """收集 S&P 500 收盤價（Yahoo Finance ^GSPC）。"""
    from collectors.fx import FXCollector

    c = FXCollector()
    data = c.collect_sp500()
    if data is None:
        return False
    c.save_sp500(date, data["close"])
    return True


def _compute_futures(date: str, conn: sqlite3.Connection) -> bool:
    """計算期貨衍生指標。"""
    from integration.futures_metrics import compute_futures_metrics

    result = compute_futures_metrics(date, conn)
    return result is not None
