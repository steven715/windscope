"""夜盤後 job：收集夜盤資料，計算期貨衍生指標。"""

import logging
import sqlite3
from datetime import datetime, timedelta

from db.connection import get_connection
from jobs.helpers import determine_status, run_step
from utils.trading_calendar import get_next_trading_day, is_trading_day

logger = logging.getLogger(__name__)


def run_after_night(date: str, db_path: str | None = None) -> dict:
    """
    夜盤後 job。收集夜盤資料，計算期貨衍生指標。

    日期語意：夜盤於凌晨 05:00 收盤，TAIFEX 將其歸屬「次一營業日」。本 job 在
    05:30 觸發，把剛收完的夜盤歸到「觸發日起算的下一個交易日」：
    - 交易日觸發（週二~五）→ 歸當日（收前一晚的夜盤）。
    - 週六觸發 → 歸下週一（收週五晚上的夜盤，補上週一早上訊號的期貨缺口）。
    這也是排程排成 tue-sat（含週六）的原因。

    回傳格式同 after_close，date 為實際歸屬的交易日。
    """
    logger.info("run_after_night: triggered on %s", date)

    # 今晨 05:00 收的夜盤，需「前一日曆日」是交易日（前一日 15:00 才會開夜盤）。
    # 週日(前日週六)、週一(前日週日)觸發都無夜盤可收。
    prev_cal = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
                ).strftime("%Y-%m-%d")
    if not is_trading_day(prev_cal):
        logger.info("run_after_night: %s 前一日非交易日，今晨無夜盤，skipping", date)
        return {"date": date, "status": "skipped", "results": {}, "errors": []}

    # 夜盤歸屬：觸發日為交易日則歸當日，否則歸下一個交易日（週六→下週一）
    target = date if is_trading_day(date) else get_next_trading_day(date)
    if target != date:
        logger.info("run_after_night: %s 非交易日，夜盤歸屬下一交易日 %s", date, target)

    results = {}
    errors = []

    with get_connection(db_path) as conn:
        # 1. TAIFEX: 夜盤收盤價 + 成交量
        ok, err = run_step("taifex_night", lambda: _collect_taifex_night(target, conn))
        results["taifex_night"] = ok
        if err:
            errors.append(err)

        # 2. FX (Yahoo Finance): S&P 500 收盤 → raw_futures.sp500_close
        ok, err = run_step("sp500_close", lambda: _collect_sp500(target, conn))
        results["sp500_close"] = ok
        if err:
            errors.append(err)

        # 3. Integration: compute_futures_metrics
        ok, err = run_step("integration_futures", lambda: _compute_futures(target, conn))
        results["integration_futures"] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_after_night: %s (target=%s) status=%s", date, target, status)
    return {"date": target, "status": status, "results": results, "errors": errors}


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
