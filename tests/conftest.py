import sqlite3

import pytest

from db.schema import create_all_tables


@pytest.fixture
def memory_db():
    """提供 in-memory SQLite，已建好所有表。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _isolate_holiday_cache(monkeypatch):
    """測試間隔離休市日快取：預設無休市日（僅週末），避免 lazy load 讀到真實 DB。

    需要測休市日的測試自行設定 trading_calendar._holiday_cache。
    """
    from utils import trading_calendar

    monkeypatch.setattr(trading_calendar, "_holiday_cache", set())
    yield
