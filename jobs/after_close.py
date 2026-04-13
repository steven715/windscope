"""收盤後 job：收集當天收盤後可用的所有資料，計算籌碼衍生指標。"""

import logging
import sqlite3

from db.connection import get_connection
from db.schema import create_all_tables
from jobs.helpers import determine_status, run_step
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_after_close(date: str, db_path: str | None = None) -> dict:
    """
    收盤後 job。收集當天收盤後可用的所有資料，計算籌碼衍生指標。

    回傳：
    {
        "date": "2026-04-08",
        "status": "completed" | "partial" | "skipped" | "failed",
        "results": {...},
        "errors": [...]
    }
    """
    logger.info("run_after_close: starting for %s", date)

    if not is_trading_day(date):
        logger.info("run_after_close: %s is not a trading day, skipping", date)
        return {"date": date, "status": "skipped", "results": {}, "errors": []}

    results = {}
    errors = []

    with get_connection(db_path) as conn:
        # 1. TWSE: 加權指數收盤價
        ok, err = run_step("twse_spot_close", lambda: _collect_twse_spot_close(date, conn))
        results["twse_spot_close"] = ok
        if err:
            errors.append(err)

        # 2. TWSE: 三大法人買賣超
        ok, err = run_step("twse_institutional", lambda: _collect_twse_institutional(date, conn))
        results["twse_institutional"] = ok
        if err:
            errors.append(err)

        # 3. TWSE: 外資個股買賣超（watchlist）
        ok, err = run_step("twse_foreign_stock", lambda: _collect_twse_foreign_stock(date, conn))
        results["twse_foreign_stock"] = ok
        if err:
            errors.append(err)

        # 4. TWSE: 個股收盤價（watchlist）
        ok, err = run_step("twse_stock_close", lambda: _collect_twse_stock_close(date, conn))
        results["twse_stock_close"] = ok
        if err:
            errors.append(err)

        # 5. TWSE: 除息預估點數
        ok, err = run_step("twse_ex_dividend", lambda: _collect_twse_ex_dividend(date, conn))
        results["twse_ex_dividend"] = ok
        if err:
            errors.append(err)

        # 6. TAIFEX: 外資期貨未平倉（STUB）
        ok, err = run_step("taifex_oi", lambda: _collect_taifex_oi(date, conn))
        results["taifex_oi"] = ok
        if err:
            errors.append(err)

        # 7. FX: USD/TWD 16:00 收盤價
        ok, err = run_step("fx_close", lambda: _collect_fx_close(date, conn))
        results["fx_close"] = ok
        if err:
            errors.append(err)

        # 8. Chip: 分點進出（STUB）
        ok, err = run_step("chip", lambda: _collect_chip(date, conn))
        results["chip"] = ok
        if err:
            errors.append(err)

        # 9. Integration: compute_chip_metrics
        ok, err = run_step("integration_chip", lambda: _compute_chip(date, conn))
        results["integration_chip"] = ok
        if err:
            errors.append(err)

    status = determine_status(results)
    logger.info("run_after_close: %s status=%s", date, status)
    return {"date": date, "status": status, "results": results, "errors": errors}


# ── 內部步驟函式 ─────────────────────────────────────────────────


def _collect_twse_spot_close(date: str, conn: sqlite3.Connection) -> bool:
    """收集加權指數收盤價。"""
    from collectors.twse import TWSECollector

    c = TWSECollector()
    data = c.collect_spot_close(date)
    if data is None:
        return False
    c.save_spot_close(date, data)
    return True


def _collect_twse_institutional(date: str, conn: sqlite3.Connection) -> bool:
    """收集三大法人買賣超。"""
    from collectors.twse import TWSECollector

    c = TWSECollector()
    data = c.collect_institutional(date)
    if data is None:
        return False
    c.save_institutional(date, data)
    return True


def _collect_twse_foreign_stock(date: str, conn: sqlite3.Connection) -> bool:
    """收集外資個股買賣超（watchlist）。"""
    from collectors.twse import TWSECollector

    c = TWSECollector()
    data = c.collect_foreign_stock(date)
    if data is None:
        return False
    c.save_foreign_stock(date, data)
    return True


def _collect_twse_stock_close(date: str, conn: sqlite3.Connection) -> bool:
    """收集個股收盤價（watchlist）。"""
    from collectors.twse import TWSECollector

    c = TWSECollector()
    data = c.collect_all_stock_close(date)
    if not data:
        return False
    c.save_stock_close(date, data)
    return True


def _collect_twse_ex_dividend(date: str, conn: sqlite3.Connection) -> bool:
    """收集除息預估點數。"""
    from collectors.twse import TWSECollector

    c = TWSECollector()
    data = c.collect_ex_dividend_points(date)
    if data is None:
        return False
    c.save_ex_dividend(date, data)
    return True


def _collect_taifex_oi(date: str, conn: sqlite3.Connection) -> bool:
    """收集外資期貨未平倉（目前 STUB）。"""
    from collectors.taifex import TAIFEXCollector

    c = TAIFEXCollector()
    data = c.collect_oi_foreign(date)
    if data is None:
        return False
    c.save_oi_foreign(date, data)
    return True


def _collect_fx_close(date: str, conn: sqlite3.Connection) -> bool:
    """收集 USD/TWD 16:00 收盤價。"""
    from collectors.fx import FXCollector

    c = FXCollector()
    data = c.collect_twd(date, "close_16")
    if data is None:
        return False
    c.save_fx(date, data["currency_pair"], data["rate"], "close_16")
    return True


def _collect_chip(date: str, conn: sqlite3.Connection) -> bool:
    """收集分點進出（目前 STUB）。"""
    from collectors.chip import ChipCollector

    c = ChipCollector()
    data = c.collect_broker_trading(date)
    if data is None:
        return False
    return True


def _compute_chip(date: str, conn: sqlite3.Connection) -> bool:
    """計算籌碼衍生指標。"""
    from integration.chip_metrics import compute_chip_metrics

    result = compute_chip_metrics(date, conn)
    # 即使沒有 chip 資料（空 list），也算成功——不是錯誤
    return True
