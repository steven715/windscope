"""開盤前 job：收集即時匯率，計算匯率指標，產出當日完整摘要。"""

import logging
import sqlite3
from datetime import datetime

from db.connection import get_connection
from jobs.helpers import JobRun
from utils.notify import notify
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_before_open(date: str, db_path: str | None = None) -> dict:
    """
    開盤前 job。收集即時匯率，計算匯率指標，產出當日完整摘要。

    台股休市日（國定假日）仍照常收集匯率並更新統計（休市無關的維度），
    但跳過訊號判斷與摘要的訊號區塊——休市無交易，不該產生可被驗證的訊號。

    回傳格式同 after_close，額外包含 summary 文字與 market_open 旗標。
    """
    trading = is_trading_day(date)
    if not trading:
        # 週末：全球市場皆休，整步略過（沿用舊行為）。
        # 平日休市（國定假日）：匯率等休市無關維度照收統計，僅跳過訊號判斷。
        if datetime.strptime(date, "%Y-%m-%d").weekday() >= 5:
            return JobRun.skipped("before_open", date, "週末",
                                  summary=None, market_open=False)
        logger.info(
            "run_before_open: %s 為台股休市日，僅收集匯率統計，跳過訊號判斷", date
        )

    from collectors.fx import FXCollector

    job = JobRun("before_open", date)
    fx = FXCollector(db_path=db_path)
    signal = None

    with get_connection(db_path) as conn:
        # 1~3, 3c. FX 即時報價（quote_0845）：來源路由收斂於 collect_and_save_pair。
        for step, pair in (("fx_twd_0845", "USD/TWD"), ("fx_cny_0845", "USD/CNY"),
                           ("fx_krw_0845", "USD/KRW"), ("fx_jpy_0845", "USD/JPY")):
            job.step(step, lambda p=pair: fx.collect_and_save_pair(date, p, "quote_0845"))

        # 3b. FX: 盤前 5 分序列（升貶節奏用，Yahoo 離岸報價；空序列視為無資料）
        job.collect("fx_intraday", lambda: fx.collect_twd_intraday() or None,
                    lambda bars: fx.save_intraday_fx(date, "USD/TWD", bars))

        # 4. Integration: compute_fx_metrics
        job.step("integration_fx", lambda: _compute_fx(date, conn))

        # 5. Layer 3: 盤前判斷 + 籌碼觀察（休市日跳過，不寫 signals 表）
        if trading:
            signal = job.step("signal", lambda: _compute_signals(date, conn))

        # 6. 產出 daily summary 文字摘要（交易日附盤前判斷區塊；休市日只更新匯率統計）
        summary_text = job.step("summary", lambda: _generate_summary(date, conn))
        if summary_text:
            if signal:
                from integration.summary import format_premarket_call_text
                summary_text = summary_text + "\n" + format_premarket_call_text(signal)
            if not trading:
                summary_text = f"📅 {date} 台股休市，僅更新匯率統計\n" + summary_text

    # signal_produced：交易日 + 確有算出盤前判斷（休市日不該有）。
    signal_produced = bool(trading and signal is not None)
    result = job.finish(
        level=logging.WARNING if (trading and not signal_produced) else logging.INFO,
        market_open=trading, signal_produced=signal_produced,
    )
    # 告警（不同於排程器的每 job 結果通知、不受通知開關控制）：只打使用者會痛的症狀——
    # 交易日卻沒產出盤前判斷。內部 step 失敗但判斷仍產出 → 不告警，只進事件供事後診斷。
    if trading and not signal_produced:
        notify(
            "⚠️ 開盤前情報：今日未產出訊號",
            f"{date} 為交易日，但 before_open 結束時未產出可用訊號"
            f"（status={result['status']}）。請查 logs/events.jsonl 的 step_run / job_completed。",
        )

    return {**result, "summary": summary_text, "market_open": trading}


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _compute_fx(date: str, conn: sqlite3.Connection) -> bool:
    """計算匯率衍生指標。"""
    from integration.fx_metrics import compute_fx_metrics

    result = compute_fx_metrics(date, conn)
    return result is not None


def _compute_signals(date: str, conn: sqlite3.Connection) -> dict | None:
    """計算盤前判斷與籌碼觀察，回傳盤前判斷（算不出來為 None＝失敗）。"""
    from integration.premarket_call import compute_premarket_call
    from integration.chip_observation import refresh_chip_observations

    result = compute_premarket_call(date, conn)
    refresh_chip_observations(date, conn)  # 分點＋外資籌碼觀察（只看 date 之前）
    return result


def _generate_summary(date: str, conn: sqlite3.Connection) -> str | None:
    """產出 daily summary 文字（無 daily_metrics 為 None＝失敗）。"""
    from integration.summary import generate_daily_summary

    return generate_daily_summary(date, conn)
