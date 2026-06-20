"""backfill 回補測試。"""

from unittest.mock import patch


def test_backfill_date_range(monkeypatch):
    """回補指定範圍，跳過週末。"""
    from jobs import backfill

    call_dates = []

    def mock_after_close(date, db_path=None):
        call_dates.append(("ac", date))
        return {"date": date, "status": "completed", "results": {"a": True}, "errors": []}

    def mock_after_night(date, db_path=None):
        call_dates.append(("an", date))
        return {"date": date, "status": "completed", "results": {"b": True}, "errors": []}

    monkeypatch.setattr(backfill, "run_after_close", mock_after_close)
    monkeypatch.setattr(backfill, "run_after_night", mock_after_night)
    monkeypatch.setattr(backfill, "run_chip_collect",
                        lambda date, db_path=None: {"date": date, "status": "completed", "results": {"c": True}, "errors": []})
    # 跳過 sleep 和 integration
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(backfill, "get_connection", _mock_conn_cm)

    result = backfill.run_backfill("2026-04-06", "2026-04-10")

    # 2026-04-06 日、04-07 一、04-08 二、04-09 三、04-10 四
    # 04-05 六 & 04-06 日 應跳過... wait, 04-06 is Monday? Let me check
    # Actually iter_trading_days skips weekends
    # 2026-04-06 is Monday, so all 5 days are trading days
    assert result["total_days"] == 5

    # 確認 after_close 和 after_night 都有被呼叫
    ac_dates = [d for t, d in call_dates if t == "ac"]
    an_dates = [d for t, d in call_dates if t == "an"]
    assert len(ac_dates) == 5
    assert len(an_dates) == 5
    # 籌碼分點收集也納入回補（補回 __PRICE_ONLY__ MA20 基礎）→ 結果合併進每日 details
    assert result["details"]["2026-04-06"]["results"].get("c") is True


def test_backfill_skips_weekends(monkeypatch):
    """回補範圍包含週末時自動跳過。"""
    from jobs import backfill

    call_dates = []

    def mock_after_close(date, db_path=None):
        call_dates.append(date)
        return {"date": date, "status": "completed", "results": {"a": True}, "errors": []}

    def mock_after_night(date, db_path=None):
        return {"date": date, "status": "completed", "results": {"b": True}, "errors": []}

    monkeypatch.setattr(backfill, "run_after_close", mock_after_close)
    monkeypatch.setattr(backfill, "run_after_night", mock_after_night)
    monkeypatch.setattr(backfill, "run_chip_collect",
                        lambda date, db_path=None: {"date": date, "status": "completed", "results": {"c": True}, "errors": []})
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(backfill, "get_connection", _mock_conn_cm)

    # 2026-04-03 (Fri) ~ 2026-04-07 (Tue) — should skip Sat 04-04 & Sun 04-05
    result = backfill.run_backfill("2026-04-03", "2026-04-07")

    assert result["total_days"] == 3  # Fri, Mon, Tue
    assert "2026-04-04" not in call_dates
    assert "2026-04-05" not in call_dates


def test_backfill_old_to_new_order(monkeypatch):
    """確認日期從舊到新執行。"""
    from jobs import backfill

    call_dates = []

    def mock_after_close(date, db_path=None):
        call_dates.append(date)
        return {"date": date, "status": "completed", "results": {"a": True}, "errors": []}

    def mock_after_night(date, db_path=None):
        return {"date": date, "status": "completed", "results": {"b": True}, "errors": []}

    monkeypatch.setattr(backfill, "run_after_close", mock_after_close)
    monkeypatch.setattr(backfill, "run_after_night", mock_after_night)
    monkeypatch.setattr(backfill, "run_chip_collect",
                        lambda date, db_path=None: {"date": date, "status": "completed", "results": {"c": True}, "errors": []})
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(backfill, "get_connection", _mock_conn_cm)

    backfill.run_backfill("2026-04-06", "2026-04-10")

    assert call_dates == sorted(call_dates)


def test_backfill_continues_on_failure(monkeypatch):
    """單日失敗不中斷整個回補流程。"""
    from jobs import backfill

    def mock_after_close(date, db_path=None):
        if date == "2026-04-07":
            return {"date": date, "status": "failed", "results": {"a": False}, "errors": ["boom"]}
        return {"date": date, "status": "completed", "results": {"a": True}, "errors": []}

    def mock_after_night(date, db_path=None):
        if date == "2026-04-07":
            return {"date": date, "status": "failed", "results": {"b": False}, "errors": ["boom"]}
        return {"date": date, "status": "completed", "results": {"b": True}, "errors": []}

    def mock_chip_collect(date, db_path=None):
        if date == "2026-04-07":
            return {"date": date, "status": "failed", "results": {"c": False}, "errors": ["boom"]}
        return {"date": date, "status": "completed", "results": {"c": True}, "errors": []}

    monkeypatch.setattr(backfill, "run_after_close", mock_after_close)
    monkeypatch.setattr(backfill, "run_after_night", mock_after_night)
    monkeypatch.setattr(backfill, "run_chip_collect", mock_chip_collect)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(backfill, "get_connection", _mock_conn_cm)

    result = backfill.run_backfill("2026-04-06", "2026-04-10")

    # 5 trading days total, 04-07 failed, rest completed
    assert result["total_days"] == 5
    assert result["failed"] == 1
    assert result["completed"] == 4
    assert result["details"]["2026-04-07"]["status"] == "failed"
    assert result["details"]["2026-04-08"]["status"] == "completed"


# ── Helper ──────────────────────────────────────────────────────

import sqlite3
from contextlib import contextmanager

from db.schema import create_all_tables


@contextmanager
def _mock_conn_cm(db_path=None):
    """Mock connection that creates in-memory DB with schema."""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
