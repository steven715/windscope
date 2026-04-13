"""FX metrics integration 測試。"""

import json
import sqlite3

import pytest

from db.schema import create_all_tables
from integration.fx_metrics import compute_fx_metrics


@pytest.fixture
def fx_db():
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    yield conn
    conn.close()


def _insert_fx(conn, date, pair, close_16, quote_0845):
    conn.execute(
        "INSERT INTO raw_fx (date, currency_pair, close_16, quote_0845) "
        "VALUES (?, ?, ?, ?)",
        (date, pair, close_16, quote_0845),
    )
    conn.commit()


class TestFxDelta:
    def test_all_bullish(self, fx_db):
        """三幣都升值 → delta 為負, direction=bullish, asia_sync=1。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.35)
        _insert_fx(fx_db, "2026-04-08", "USD/CNY", 7.2500, 7.2400)
        _insert_fx(fx_db, "2026-04-08", "USD/KRW", 1380.0, 1370.0)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result is not None
        assert result["fx_delta_twd"] == pytest.approx(-0.15)
        assert result["fx_delta_cny"] == pytest.approx(-0.01)
        assert result["fx_delta_krw"] == pytest.approx(-10.0)
        assert result["fx_direction"] == "bullish"
        assert result["fx_asia_sync"] == 1

    def test_all_bearish(self, fx_db):
        """三幣都貶值 → asia_sync=1, direction=bearish。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.00, 31.20)
        _insert_fx(fx_db, "2026-04-08", "USD/CNY", 7.2000, 7.2100)
        _insert_fx(fx_db, "2026-04-08", "USD/KRW", 1350.0, 1360.0)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_direction"] == "bearish"
        assert result["fx_asia_sync"] == 1

    def test_mixed_directions(self, fx_db):
        """TWD bullish, CNY bearish → asia_sync=0。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.35)
        _insert_fx(fx_db, "2026-04-08", "USD/CNY", 7.2500, 7.2600)
        _insert_fx(fx_db, "2026-04-08", "USD/KRW", 1380.0, 1370.0)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_asia_sync"] == 0
        detail = json.loads(result["fx_asia_detail"])
        assert detail["TWD"] == "bullish"
        assert detail["CNY"] == "bearish"
        assert detail["KRW"] == "bullish"

    def test_partial_null_quote(self, fx_db):
        """CNY quote_0845 為 NULL → cny delta None。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.35)
        _insert_fx(fx_db, "2026-04-08", "USD/CNY", 7.2500, None)
        _insert_fx(fx_db, "2026-04-08", "USD/KRW", 1380.0, 1370.0)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_delta_cny"] is None
        # TWD=bullish, KRW=bullish, CNY=None → 2 non-None, same direction
        assert result["fx_asia_sync"] == 0  # not all 3 present

    def test_all_null_no_fx_data(self, fx_db):
        """完全無 FX 資料 → 回傳 None。"""
        result = compute_fx_metrics("2026-04-08", fx_db)
        assert result is None

    def test_all_currencies_null_values(self, fx_db):
        """三幣都有 row 但 quote/close 都是 NULL → 全部 delta None。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", None, None)
        _insert_fx(fx_db, "2026-04-08", "USD/CNY", None, None)
        _insert_fx(fx_db, "2026-04-08", "USD/KRW", None, None)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result is not None  # rows exist, return result with Nones
        assert result["fx_delta_twd"] is None
        assert result["fx_direction"] is None
        assert result["fx_asia_sync"] is None

    def test_boundary_neutral_negative(self, fx_db):
        """delta = -0.10（剛好在門檻上）→ neutral。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.40)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_direction"] == "neutral"

    def test_boundary_neutral_positive(self, fx_db):
        """delta = +0.10（剛好在門檻上）→ neutral。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.60)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_direction"] == "neutral"

    def test_boundary_bearish(self, fx_db):
        """delta 剛好超過門檻 → bearish。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.61)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_direction"] == "bearish"

    def test_only_twd_data(self, fx_db):
        """只有 TWD 資料 → asia_sync=None（資料不足）。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.35)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_delta_twd"] == pytest.approx(-0.15)
        assert result["fx_delta_cny"] is None
        assert result["fx_delta_krw"] is None
        assert result["fx_asia_sync"] is None


class TestFxWriteToDb:
    def test_writes_to_daily_metrics(self, fx_db):
        """確認寫入 daily_metrics。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.35)

        compute_fx_metrics("2026-04-08", fx_db)

        row = fx_db.execute(
            "SELECT fx_delta_twd, fx_direction FROM daily_metrics WHERE date = ?",
            ("2026-04-08",),
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(-0.15)
        assert row[1] == "bullish"

    def test_does_not_overwrite_futures_columns(self, fx_db):
        """寫入 FX 欄位不覆蓋期貨欄位。"""
        fx_db.execute(
            "INSERT INTO daily_metrics (date, futures_spread) VALUES (?, ?)",
            ("2026-04-08", 100.0),
        )
        fx_db.commit()

        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.35)
        compute_fx_metrics("2026-04-08", fx_db)

        row = fx_db.execute(
            "SELECT fx_delta_twd, futures_spread FROM daily_metrics WHERE date = ?",
            ("2026-04-08",),
        ).fetchone()
        assert row[0] == pytest.approx(-0.15)
        assert row[1] == pytest.approx(100.0)  # preserved

    def test_idempotent_write(self, fx_db):
        """重複計算同一天不報錯，資料為最新。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.35)
        compute_fx_metrics("2026-04-08", fx_db)

        # Update raw data and recompute
        fx_db.execute(
            "UPDATE raw_fx SET quote_0845 = 31.40 "
            "WHERE date = '2026-04-08' AND currency_pair = 'USD/TWD'"
        )
        fx_db.commit()
        compute_fx_metrics("2026-04-08", fx_db)

        row = fx_db.execute(
            "SELECT fx_delta_twd FROM daily_metrics WHERE date = ?",
            ("2026-04-08",),
        ).fetchone()
        assert row[0] == pytest.approx(-0.10)
