"""trading_calendar 工具測試。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from utils import trading_calendar
from utils.trading_calendar import (
    get_next_trading_day,
    get_previous_trading_day,
    get_recent_trading_days,
    is_market_holiday,
    is_trading_day,
    refresh_holiday_cache,
)


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


class TestPreviousTradingDaySkipsHolidayRow:
    """回歸測試：raw_futures 混入休市日空殼 row 時，前一交易日不可抓到休市日。

    重現 bug：週一(06-22)的前一交易日應為週四(06-18)，而非端午休市的週五(06-19)
    —— 即使 raw_futures 有一筆 06-19 的空殼 row（休市日除息預設值寫入造成）。
    """

    @pytest.fixture(autouse=True)
    def _set_holiday(self, monkeypatch):
        monkeypatch.setattr(trading_calendar, "_holiday_cache", {"2026-06-19"})

    @pytest.fixture
    def db_with_holiday_row(self):
        conn = sqlite3.connect(":memory:")
        create_all_tables(conn)
        # 06-17、06-18 為正常交易日；06-19 為端午休市但有一筆空殼 row（污染）
        for d in ("2026-06-17", "2026-06-18"):
            conn.execute("INSERT INTO raw_futures (date) VALUES (?)", (d,))
        conn.execute(
            "INSERT INTO raw_futures (date, ex_dividend_points) VALUES "
            "('2026-06-19', 0.0)"  # 休市日空殼 row（只有除息預設值）
        )
        conn.commit()
        yield conn
        conn.close()

    def test_previous_skips_holiday_placeholder(self, db_with_holiday_row):
        # 06-22(週一) → 跳過 06-19(休市空殼) → 06-18(週四)
        assert get_previous_trading_day(
            "2026-06-22", db_with_holiday_row) == "2026-06-18"

    def test_recent_skips_holiday_placeholder(self, db_with_holiday_row):
        # 近 2 交易日不含 06-19
        assert get_recent_trading_days(
            "2026-06-22", 2, db_with_holiday_row) == ["2026-06-18", "2026-06-17"]


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
