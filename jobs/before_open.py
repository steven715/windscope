"""開盤前 job：收集即時匯率，計算匯率指標，產出當日完整摘要。"""

import logging
import sqlite3
from datetime import datetime

from db.connection import get_connection
from jobs.helpers import determine_status, run_step
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_before_open(date: str, db_path: str | None = None) -> dict:
    """
    開盤前 job。收集即時匯率，計算匯率指標，產出當日完整摘要。

    台股休市日（國定假日）仍照常收集匯率並更新統計（休市無關的維度），
    但跳過訊號判斷與摘要的訊號區塊——休市無交易，不該產生可被驗證的訊號。

    回傳格式同 after_close，額外包含 summary 文字與 market_open 旗標。
    """
    logger.info("run_before_open: starting for %s", date)

    trading = is_trading_day(date)
    if not trading:
        # 週末：全球市場皆休，整步略過（沿用舊行為）。
        # 平日休市（國定假日）：匯率等休市無關維度照收統計，僅跳過訊號判斷。
        if datetime.strptime(date, "%Y-%m-%d").weekday() >= 5:
            logger.info("run_before_open: %s 為週末，skipping", date)
            return {
                "date": date, "status": "skipped",
                "results": {}, "errors": [], "summary": None,
                "market_open": False,
            }
        logger.info(
            "run_before_open: %s 為台股休市日，僅收集匯率統計，跳過訊號判斷", date
        )

    results = {}
    errors = []
    summary_text = None

    from collectors.fx import FXCollector

    with get_connection(db_path) as conn:
        fx = FXCollector(db_path=db_path)

        # 1~3, 3c. FX 即時報價（quote_0845）：來源路由收斂於 collect_and_save_pair。
        for step, pair in (("fx_twd_0845", "USD/TWD"), ("fx_cny_0845", "USD/CNY"),
                           ("fx_krw_0845", "USD/KRW"), ("fx_jpy_0845", "USD/JPY")):
            ok, err = run_step(
                step, lambda p=pair: fx.collect_and_save_pair(date, p, "quote_0845"))
            results[step] = ok
            if err:
                errors.append(err)

        # 3b. FX: 盤前 5 分序列（升貶節奏用，Yahoo 離岸報價）
        ok, err = run_step("fx_intraday", lambda: _collect_fx_intraday(date, conn))
        results["fx_intraday"] = ok
        if err:
            errors.append(err)

        # 4. Integration: compute_fx_metrics
        ok, err = run_step("integration_fx", lambda: _compute_fx(date, conn))
        results["integration_fx"] = ok
        if err:
            errors.append(err)

        # 5. Layer 3: 市場訊號 + 個股觀察訊號（休市日跳過，不寫 signals 表）
        signal = None
        if trading:
            ok, err = run_step("signal", lambda: _compute_signals(date, conn))
            if ok:
                from integration.signal_engine import compute_market_signal
                signal = compute_market_signal(date, conn)
            results["signal"] = ok
            if err:
                errors.append(err)

        # 6. 產出 daily summary 文字摘要（交易日附訊號區塊；休市日只更新匯率統計）
        ok, err = run_step("summary", lambda: _generate_summary(date, conn))
        if ok:
            from integration.summary import generate_daily_summary
            summary_text = generate_daily_summary(date, conn)
            if summary_text and signal:
                from integration.signal_engine import format_signal_text
                summary_text = summary_text + "\n" + format_signal_text(signal)
            if summary_text and not trading:
                summary_text = f"📅 {date} 台股休市，僅更新匯率統計\n" + summary_text
        results["summary"] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_before_open: %s status=%s (market_open=%s)", date, status, trading)
    return {
        "date": date, "status": status,
        "results": results, "errors": errors,
        "summary": summary_text, "market_open": trading,
    }


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _collect_fx_intraday(date: str, conn: sqlite3.Connection) -> bool:
    """收集 USD/TWD 盤前 5 分序列（升貶節奏用）。"""
    from collectors.fx import FXCollector

    c = FXCollector()
    bars = c.collect_twd_intraday()
    if not bars:
        return False
    c.save_intraday_fx(date, "USD/TWD", bars)
    return True


def _compute_fx(date: str, conn: sqlite3.Connection) -> bool:
    """計算匯率衍生指標。"""
    from integration.fx_metrics import compute_fx_metrics

    result = compute_fx_metrics(date, conn)
    return result is not None


def _compute_signals(date: str, conn: sqlite3.Connection) -> bool:
    """計算市場訊號與個股觀察訊號。市場訊號算不出來視為失敗。"""
    from integration.signal_engine import (
        compute_foreign_stock_signals,
        compute_market_signal,
        compute_stock_signals,
    )

    result = compute_market_signal(date, conn)
    compute_stock_signals(date, conn)          # 分點訊號（需籌碼資料）
    compute_foreign_stock_signals(date, conn)  # 外資流向訊號（用 T86，免費自動）
    return result is not None


def _generate_summary(date: str, conn: sqlite3.Connection) -> bool:
    """產出 daily summary，驗證不報錯即成功。"""
    from integration.summary import generate_daily_summary

    text = generate_daily_summary(date, conn)
    return text is not None
