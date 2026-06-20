"""收盤後 job：收集當天收盤後的「市場面」資料，供隔日基準。

含：加權指數收盤(spot_close，隔日 after_night 算期貨缺口的基準)、三大法人、外資個股、
除息預估點數、外資期貨未平倉。
拆出去的部分：個股收盤＋分點＋籌碼指標 → chip_collect job；FX 16:00 收盤 → afternoon_fx(16:00)。
"""

import logging
import sqlite3

from db.connection import get_connection
from jobs.helpers import determine_status, run_step
from utils.trading_calendar import is_trading_day

logger = logging.getLogger(__name__)


def run_after_close(date: str, db_path: str | None = None) -> dict:
    """
    收盤後 job。收集收盤後的市場面資料（指數收盤／三大法人／外資個股／除息／期貨未平倉）。

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
        # 1. TWSE: 加權指數收盤價（spot_close）——隔日 after_night 算期貨夜盤缺口的基準
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

        # 4. TWSE: 除息預估點數
        ok, err = run_step("twse_ex_dividend", lambda: _collect_twse_ex_dividend(date, conn))
        results["twse_ex_dividend"] = ok
        if err:
            errors.append(err)

        # 5. TAIFEX: 外資期貨未平倉
        ok, err = run_step("taifex_oi", lambda: _collect_taifex_oi(date, conn))
        results["taifex_oi"] = ok
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
    """收集外資期貨未平倉。"""
    from collectors.taifex import TAIFEXCollector

    c = TAIFEXCollector()
    data = c.collect_oi_foreign(date)
    if data is None:
        return False
    c.save_oi_foreign(date, data)
    return True
