"""trading_calendar 工具測試。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from utils.trading_calendar import get_previous_trading_day, get_recent_trading_days


@pytest.fixture
def cal_db():
    """建立含交易日資料的 in-memory DB。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    for d in [
        "2026-04-01", "2026-04-02", "2026-04-03",
        "2026-04-07", "2026-04-08", "2026-04-09",
    ]:
        conn.execute("INSERT INTO raw_futures (date) VALUES (?)", (d,))
    conn.commit()
    yield conn
    conn.close()


class TestGetPreviousTradingDay:
    def test_normal(self, cal_db):
        assert get_previous_trading_day("2026-04-09", cal_db) == "2026-04-08"

    def test_after_weekend(self, cal_db):
        assert get_previous_trading_day("2026-04-07", cal_db) == "2026-04-03"

    def test_no_db_history_falls_to_weekday_fallback(self, cal_db):
        """DB 無更早資料時 fallback 到跳過週末。"""
        result = get_previous_trading_day("2026-03-01", cal_db)
        # 2026-03-01 是星期日，fallback 到 02-27（五）
        assert result == "2026-02-27"

    def test_fallback_skip_weekend_no_db(self):
        """無 DB 連線時 fallback 跳過週末。"""
        # 2026-04-06 是星期一，前一個 weekday 是 04-03 (五)
        result = get_previous_trading_day("2026-04-06")
        assert result == "2026-04-03"

    def test_fallback_from_institutional(self):
        """raw_futures 無資料但 raw_institutional 有。"""
        conn = sqlite3.connect(":memory:")
        create_all_tables(conn)
        conn.execute(
            "INSERT INTO raw_institutional (date) VALUES (?)", ("2026-04-08",)
        )
        conn.commit()
        result = get_previous_trading_day("2026-04-09", conn)
        assert result == "2026-04-08"
        conn.close()


class TestGetRecentTradingDays:
    def test_normal(self, cal_db):
        result = get_recent_trading_days("2026-04-09", 3, cal_db)
        assert result == ["2026-04-08", "2026-04-07", "2026-04-03"]

    def test_fewer_than_n(self, cal_db):
        result = get_recent_trading_days("2026-04-02", 5, cal_db)
        assert result == ["2026-04-01"]

    def test_no_db_history_falls_to_weekday_fallback(self, cal_db):
        """DB 無更早資料時 fallback 到跳過週末。"""
        result = get_recent_trading_days("2026-03-01", 3, cal_db)
        assert result == ["2026-02-27", "2026-02-26", "2026-02-25"]

    def test_fallback_no_db(self):
        """無 DB 時 fallback 跳過週末。"""
        result = get_recent_trading_days("2026-04-09", 3)
        assert result == ["2026-04-08", "2026-04-07", "2026-04-06"]
