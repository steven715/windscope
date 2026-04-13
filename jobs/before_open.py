"""開盤前 job：收集即時匯率，計算匯率指標，產出當日完整摘要。"""

import logging
import sqlite3

from db.connection import get_connection
from jobs.helpers import determine_status, run_step
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_before_open(date: str, db_path: str | None = None) -> dict:
    """
    開盤前 job。收集即時匯率，計算匯率指標，產出當日完整摘要。

    回傳格式同 after_close，額外包含 summary 文字。
    """
    logger.info("run_before_open: starting for %s", date)

    if not is_trading_day(date):
        logger.info("run_before_open: %s is not a trading day, skipping", date)
        return {
            "date": date, "status": "skipped",
            "results": {}, "errors": [], "summary": None,
        }

    results = {}
    errors = []
    summary_text = None

    with get_connection(db_path) as conn:
        # 1. FX: USD/TWD 08:45 即時報價
        ok, err = run_step("fx_twd_0845", lambda: _collect_fx_quote(date, "USD/TWD", conn))
        results["fx_twd_0845"] = ok
        if err:
            errors.append(err)

        # 2. FX: USD/CNY 即時報價
        ok, err = run_step("fx_cny_0845", lambda: _collect_fx_cny(date, conn))
        results["fx_cny_0845"] = ok
        if err:
            errors.append(err)

        # 3. FX: USD/KRW 即時報價
        ok, err = run_step("fx_krw_0845", lambda: _collect_fx_krw(date, conn))
        results["fx_krw_0845"] = ok
        if err:
            errors.append(err)

        # 4. Integration: compute_fx_metrics
        ok, err = run_step("integration_fx", lambda: _compute_fx(date, conn))
        results["integration_fx"] = ok
        if err:
            errors.append(err)

        # 5. 產出 daily summary 文字摘要
        ok, err = run_step("summary", lambda: _generate_summary(date, conn))
        if ok:
            from integration.summary import generate_daily_summary
            summary_text = generate_daily_summary(date, conn)
        results["summary"] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_before_open: %s status=%s", date, status)
    return {
        "date": date, "status": status,
        "results": results, "errors": errors,
        "summary": summary_text,
    }


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _collect_fx_quote(date: str, pair: str, conn: sqlite3.Connection) -> bool:
    """收集 USD/TWD 08:45 即時報價。"""
    from collectors.fx import FXCollector

    c = FXCollector()
    data = c.collect_twd(date, "quote_0845")
    if data is None:
        return False
    c.save_fx(date, data["currency_pair"], data["rate"], "quote_0845")
    return True


def _collect_fx_cny(date: str, conn: sqlite3.Connection) -> bool:
    """收集 USD/CNY 即時報價。"""
    from collectors.fx import FXCollector

    c = FXCollector()
    data = c.collect_foreign_fx("USD/CNY")
    if data is None:
        return False
    c.save_fx(date, data["currency_pair"], data["rate"], "quote_0845")
    return True


def _collect_fx_krw(date: str, conn: sqlite3.Connection) -> bool:
    """收集 USD/KRW 即時報價。"""
    from collectors.fx import FXCollector

    c = FXCollector()
    data = c.collect_foreign_fx("USD/KRW")
    if data is None:
        return False
    c.save_fx(date, data["currency_pair"], data["rate"], "quote_0845")
    return True


def _compute_fx(date: str, conn: sqlite3.Connection) -> bool:
    """計算匯率衍生指標。"""
    from integration.fx_metrics import compute_fx_metrics

    result = compute_fx_metrics(date, conn)
    return result is not None


def _generate_summary(date: str, conn: sqlite3.Connection) -> bool:
    """產出 daily summary，驗證不報錯即成功。"""
    from integration.summary import generate_daily_summary

    text = generate_daily_summary(date, conn)
    return text is not None
