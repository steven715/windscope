"""Futures metrics integration 測試。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from integration.futures_metrics import compute_futures_metrics


@pytest.fixture
def fut_db():
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    yield conn
    conn.close()


def _seed_history(conn, volumes):
    """塞入歷史夜盤成交量（日期自動遞增）。"""
    dates = [
        "2026-04-01", "2026-04-02", "2026-04-03",
        "2026-04-04", "2026-04-07",
    ]
    for d, vol in zip(dates, volumes):
        conn.execute(
            "INSERT INTO raw_futures (date, night_volume) VALUES (?, ?)",
            (d, vol),
        )
    conn.commit()


def _insert_target(conn, date="2026-04-08", **kwargs):
    """塞入目標日的 raw_futures。"""
    cols = ["date"] + list(kwargs.keys())
    vals = [date] + list(kwargs.values())
    placeholders = ", ".join(["?"] * len(vals))
    col_names = ", ".join(cols)
    conn.execute(
        f"INSERT INTO raw_futures ({col_names}) VALUES ({placeholders})",
        vals,
    )
    conn.commit()


class TestFuturesSpread:
    def test_normal_spread(self, fut_db):
        """正常計算 spread = night_close - spot_close。"""
        _seed_history(fut_db, [30000, 35000, 32000, 28000, 31000])
        _insert_target(
            fut_db, night_close=20150.0, night_volume=45000,
            spot_close=20050.0, ex_dividend_points=30.0,
        )

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result is not None
        assert result["futures_spread"] == pytest.approx(100.0)
        assert result["futures_spread_adjusted"] == pytest.approx(70.0)
        # avg = (30000+35000+32000+28000+31000)/5 = 31200
        assert result["futures_volume_ratio"] == pytest.approx(45000 / 31200, rel=0.01)

    def test_ex_dividend_null_fallback(self, fut_db):
        """除息點數 NULL → adjusted = raw spread。"""
        _insert_target(
            fut_db, night_close=20150.0, night_volume=10000,
            spot_close=20050.0,
        )

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["futures_spread"] == pytest.approx(100.0)
        assert result["futures_spread_adjusted"] == pytest.approx(100.0)

    def test_night_close_null(self, fut_db):
        """night_close 為 NULL → spread = None, adjusted = None。"""
        _insert_target(fut_db, spot_close=20050.0, night_volume=10000)

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["futures_spread"] is None
        assert result["futures_spread_adjusted"] is None

    def test_spot_close_null(self, fut_db):
        """spot_close 為 NULL → spread = None。"""
        _insert_target(fut_db, night_close=20150.0, night_volume=10000)

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["futures_spread"] is None

    def test_no_data(self, fut_db):
        """完全無期貨資料 → 回傳 None。"""
        result = compute_futures_metrics("2026-04-08", fut_db)
        assert result is None


class TestFuturesVolumeRatio:
    def test_partial_history(self, fut_db):
        """歷史不足 5 天 → 用現有天數算。"""
        # Only 2 days of history
        fut_db.execute(
            "INSERT INTO raw_futures (date, night_volume) VALUES (?, ?)",
            ("2026-04-07", 30000),
        )
        fut_db.commit()
        _insert_target(fut_db, night_close=20000.0, spot_close=19950.0,
                       night_volume=45000)

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["futures_volume_ratio"] == pytest.approx(45000 / 30000, rel=0.01)

    def test_zero_history(self, fut_db):
        """無歷史資料 → volume_ratio = None。"""
        _insert_target(fut_db, night_close=20000.0, spot_close=19950.0,
                       night_volume=45000)

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["futures_volume_ratio"] is None

    def test_night_volume_null(self, fut_db):
        """今日 night_volume NULL → volume_ratio = None。"""
        _seed_history(fut_db, [30000, 35000, 32000, 28000, 31000])
        _insert_target(fut_db, night_close=20000.0, spot_close=19950.0)

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["futures_volume_ratio"] is None


class TestFuturesOI:
    def test_oi_stub_null(self, fut_db):
        """OI 為 STUB 狀態 → oi_net_foreign=None, oi_delta=None。"""
        _insert_target(fut_db, night_close=20000.0, spot_close=19950.0,
                       night_volume=10000)

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["oi_net_foreign"] is None
        assert result["oi_delta"] is None

    def test_oi_with_previous(self, fut_db):
        """OI 有值且前一天也有 → 計算 delta。"""
        fut_db.execute(
            "INSERT INTO raw_futures (date, oi_net_foreign, night_close, spot_close) "
            "VALUES (?, ?, ?, ?)",
            ("2026-04-07", 50000, 20000.0, 19950.0),
        )
        fut_db.commit()
        _insert_target(fut_db, night_close=20100.0, spot_close=20050.0,
                       night_volume=10000, oi_net_foreign=52000)

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["oi_net_foreign"] == 52000
        assert result["oi_delta"] == 2000

    def test_oi_no_previous(self, fut_db):
        """OI 有值但無前一天 → oi_delta=None。"""
        _insert_target(fut_db, night_close=20100.0, spot_close=20050.0,
                       night_volume=10000, oi_net_foreign=52000)

        result = compute_futures_metrics("2026-04-08", fut_db)

        assert result["oi_net_foreign"] == 52000
        assert result["oi_delta"] is None


class TestFuturesWriteToDb:
    def test_writes_to_daily_metrics(self, fut_db):
        """確認寫入 daily_metrics。"""
        _insert_target(fut_db, night_close=20150.0, spot_close=20050.0,
                       night_volume=10000)

        compute_futures_metrics("2026-04-08", fut_db)

        row = fut_db.execute(
            "SELECT futures_spread, futures_spread_adjusted "
            "FROM daily_metrics WHERE date = ?",
            ("2026-04-08",),
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(100.0)

    def test_does_not_overwrite_fx_columns(self, fut_db):
        """寫入期貨欄位不覆蓋 FX 欄位。"""
        fut_db.execute(
            "INSERT INTO daily_metrics (date, fx_delta_twd) VALUES (?, ?)",
            ("2026-04-08", -0.15),
        )
        fut_db.commit()
        _insert_target(fut_db, night_close=20150.0, spot_close=20050.0,
                       night_volume=10000)

        compute_futures_metrics("2026-04-08", fut_db)

        row = fut_db.execute(
            "SELECT fx_delta_twd, futures_spread FROM daily_metrics WHERE date = ?",
            ("2026-04-08",),
        ).fetchone()
        assert row[0] == pytest.approx(-0.15)  # preserved
        assert row[1] == pytest.approx(100.0)
