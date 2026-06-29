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

    # 1b. 確保前一交易日的指數基準存在：缺則從 MI_5MINS_HIST 補。
    #     避免單日驗證漏跑造成隔日驗證因「無前日收盤」連環失敗。
    ok, err = run_step("prev_index_baseline",
                       lambda: _ensure_prev_index_baseline(date, db_path))
    results["prev_index_baseline"] = ok
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

        # 2b. 自癒：補驗最近「有訊號卻無 verification」的交易日。
        #     某日 14:30 指數尚未發布 → 當日 verify partial、無驗證列；隔日
        #     _ensure_prev_index_baseline 會補回該日指數，此步隨後補上驗證，
        #     避免訊號靜默掉出命中率統計（如 2026-06-25）。
        ok, err = run_step("backfill_unverified",
                           lambda: _backfill_unverified_signals(date, conn))
        results["backfill_unverified"] = ok
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


def _ensure_prev_index_baseline(date: str, db_path: str | None) -> bool:
    """確保前一交易日的 raw_index 收盤基準存在，缺則從 MI_5MINS_HIST 補。

    回傳 True：基準已存在或補齊成功。False：無前一交易日或補不到。
    """
    from collectors.twse import TWSECollector
    from utils.trading_calendar import get_previous_trading_day

    with get_connection(db_path) as conn:
        prev_day = get_previous_trading_day(date, conn)
        if prev_day is None:
            return False
        row = conn.execute(
            "SELECT close FROM raw_index WHERE date = ?", (prev_day,)
        ).fetchone()
        if row is not None and row[0] is not None:
            return True  # 基準已存在，免補

    c = TWSECollector(db_path=db_path)
    data = c.collect_index_ohlc(prev_day)
    if data is None:
        logger.warning("_ensure_prev_index_baseline: 無法回補 %s 指數基準", prev_day)
        return False
    c.save_index_ohlc(prev_day, data)
    logger.info("_ensure_prev_index_baseline: 已回補 raw_index %s", prev_day)
    return True


def _verify(date: str, conn: sqlite3.Connection) -> bool:
    """執行訊號驗證。"""
    from integration.verification import verify_signal

    result = verify_signal(date, conn)
    return result is not None


def _backfill_unverified_signals(date: str, conn: sqlite3.Connection,
                                 lookback_days: int = 10) -> bool:
    """補驗最近 lookback_days 天內「有訊號卻無 verification」的交易日。

    回傳 True（補了幾筆都算成功，無事可補也回 True）；個別日仍缺指數則略過，
    留待下次再試。只處理 date 之前的日子（當日由主驗證步驟負責）。
    """
    from datetime import datetime, timedelta

    from integration.verification import verify_signal

    start = (datetime.strptime(date, "%Y-%m-%d")
             - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """SELECT s.date FROM signals s
           LEFT JOIN verifications v ON s.date = v.date
           WHERE v.date IS NULL AND s.date < ? AND s.date >= ?
           ORDER BY s.date""",
        (date, start),
    ).fetchall()

    backfilled = 0
    for (d,) in rows:
        if verify_signal(d, conn) is not None:
            backfilled += 1
            logger.info("_backfill_unverified_signals: 補驗 %s", d)
    if backfilled:
        logger.info("_backfill_unverified_signals: 共補驗 %d 筆", backfilled)
    return True
