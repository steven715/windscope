"""夜盤後 job：收集夜盤資料，計算期貨衍生指標。"""

import logging
import sqlite3
from datetime import datetime, timedelta

from db.connection import get_connection
from jobs.helpers import JobRun
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
    # 今晨 05:00 收的夜盤，需「前一日曆日」是交易日（前一日 15:00 才會開夜盤）。
    # 週日(前日週六)、週一(前日週日)觸發都無夜盤可收。
    prev_cal = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)
                ).strftime("%Y-%m-%d")
    if not is_trading_day(prev_cal):
        return JobRun.skipped("after_night", date, "前一日非交易日，今晨無夜盤")

    # 夜盤歸屬：觸發日為交易日則歸當日，否則歸下一個交易日（週六→下週一）
    target = date if is_trading_day(date) else get_next_trading_day(date)
    if target != date:
        logger.info("run_after_night: %s 非交易日，夜盤歸屬下一交易日 %s", date, target)

    from collectors.fx import FXCollector
    from collectors.taifex import TAIFEXCollector

    job = JobRun("after_night", target)
    taifex = TAIFEXCollector(db_path=db_path)
    fx = FXCollector(db_path=db_path)

    with get_connection(db_path) as conn:
        # 1. TAIFEX: 夜盤收盤價 + 成交量
        job.collect("taifex_night", lambda: taifex.collect_night_session(target),
                    lambda d: taifex.save_night_session(target, d))
        # 2. FX (Yahoo Finance): S&P 500 收盤 → raw_futures.sp500_close
        job.collect("sp500_close", fx.collect_sp500,
                    lambda d: fx.save_sp500(target, d["close"]))
        # 3. Integration: compute_futures_metrics
        job.step("integration_futures", lambda: _compute_futures(target, conn))

    return job.finish(trigger_date=date)


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _compute_futures(date: str, conn: sqlite3.Connection) -> bool:
    """計算期貨衍生指標。"""
    from integration.futures_metrics import compute_futures_metrics

    result = compute_futures_metrics(date, conn)
    return result is not None
