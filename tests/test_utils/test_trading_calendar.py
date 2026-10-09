"""trading_calendar 工具測試。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from utils import trading_calendar
from utils.trading_calendar import (
    get_next_trading_day,
    get_previous_trading_day,
    is_market_holiday,
    is_trading_day,
    refresh_holiday_cache,
)


class TestGetPreviousTradingDay:
    """僅週末情境（conftest 預設清空休市快取）；日期不代表真實休市日曆。"""

    def test_normal(self):
        assert get_previous_trading_day("2026-04-09") == "2026-04-08"

    def test_after_weekend(self):
        # 2026-04-06 是星期一，前一個 weekday 是 04-03 (五)
        assert get_previous_trading_day("2026-04-06") == "2026-04-03"

    def test_from_sunday(self):
        # 2026-03-01 是星期日 → 02-27（五）
        assert get_previous_trading_day("2026-03-01") == "2026-02-27"


class TestHolidayAwareness:
    """休市日感知：is_trading_day / is_market_holiday / get_next_trading_day。

    autouse fixture 預設清空快取，這裡顯式塞入端午節 2026-06-19 作為休市日。
    """

    @pytest.fixture(autouse=True)
    def _set_holiday(self, monkeypatch):
        monkeypatch.setattr(trading_calendar, "_holiday_cache", {"2026-06-19"})

    def test_is_market_holiday(self):
        assert is_market_holiday("2026-06-19") is True
        assert is_market_holiday("2026-06-18") is False

    def test_trading_day_false_on_holiday(self):
        # 2026-06-19 是星期五但為端午節休市
        assert is_trading_day("2026-06-19") is False

    def test_trading_day_true_on_normal_weekday(self):
        assert is_trading_day("2026-06-18") is True

    def test_trading_day_false_on_weekend(self):
        assert is_trading_day("2026-06-20") is False  # 星期六

    def test_next_trading_day_skips_holiday(self):
        # 2026-06-18(四) 之後跳過 06-19(端午) → 06-22(週一)
        assert get_next_trading_day("2026-06-18") == "2026-06-22"


def test_refresh_holiday_cache_reads_db(tmp_path):
    """refresh_holiday_cache 從 market_holidays 載入並讓 is_trading_day 生效。"""
    db = str(tmp_path / "test.db")
    conn = sqlite3.connect(db)
    create_all_tables(conn)
    conn.execute(
        "INSERT INTO market_holidays (date, name) VALUES ('2026-06-19', '端午節')"
    )
    conn.commit()
    conn.close()

    n = refresh_holiday_cache(db)
    assert n == 1
    assert is_market_holiday("2026-06-19") is True
    assert is_trading_day("2026-06-19") is False


def test_load_holidays_missing_table_returns_empty(tmp_path):
    """DB 無 market_holidays 表時退回空集合（僅排除週末），不報錯。"""
    db = str(tmp_path / "empty.db")
    sqlite3.connect(db).close()  # 建空 DB，無任何表
    assert refresh_holiday_cache(db) == 0
    assert is_trading_day("2026-06-19") is True  # 週五，無休市資料 → 視為交易日


class TestPreviousTradingDaySkipsHoliday:
    """前一交易日跳過休市日：週一(06-22)的前一交易日為週四(06-18)，非端午(06-19)。"""

    @pytest.fixture(autouse=True)
    def _set_holiday(self, monkeypatch):
        monkeypatch.setattr(trading_calendar, "_holiday_cache", {"2026-06-19"})

    def test_previous_skips_holiday(self):
        assert get_previous_trading_day("2026-06-22") == "2026-06-18"


class TestYearCoverageWarning:
    """休市表無該年資料時記一次 WARNING（每年只一次）。"""

    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(trading_calendar, "_holiday_cache", {"2026-06-19"})
        monkeypatch.setattr(trading_calendar, "_warned_years", set())

    def test_warns_once_for_uncovered_year(self, caplog):
        with caplog.at_level("WARNING", logger="utils.trading_calendar"):
            is_trading_day("2027-01-04")
            is_trading_day("2027-01-05")
        msgs = [r for r in caplog.records if "2027" in r.getMessage()]
        assert len(msgs) == 1

    def test_no_warning_for_covered_year(self, caplog):
        with caplog.at_level("WARNING", logger="utils.trading_calendar"):
            is_trading_day("2026-06-18")
        assert not caplog.records
