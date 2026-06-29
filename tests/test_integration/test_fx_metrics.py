"""FX metrics integration 測試。

TWD 隔夜 delta 走離岸 USDTWD=X 晨對晨（intraday_fx 最後一根）；CNY/KRW 仍走在岸
08:45 報價 − 前一交易日 16:00 收盤。故 TWD 的測試以 intraday_fx 餵資料。
"""

import json
import sqlite3

import pytest

from db.schema import create_all_tables
from integration.fx_metrics import compute_fx_metrics
from utils.trading_calendar import get_previous_trading_day


@pytest.fixture
def fx_db():
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    yield conn
    conn.close()


def _seed_twd_offshore(conn, date, morning_close):
    """灌某日離岸 USD/TWD 晨間 intraday 序列：最後一根（最大 ts）= morning_close。"""
    if morning_close is None:
        conn.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                     "VALUES (?, 'USD/TWD', 200, NULL)", (date,))
        return
    conn.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                 "VALUES (?, 'USD/TWD', 100, ?)", (date, morning_close - 0.02))
    conn.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                 "VALUES (?, 'USD/TWD', 200, ?)", (date, morning_close))


def _insert_fx(conn, date, pair, close_16, quote_0845):
    """raw_fx：close_16 屬前一交易日(基準)、quote_0845 屬當日今早報價。

    TWD 另以同數值灌離岸晨對晨 intraday（TWD delta 真實來源）；raw_fx row 仍留著，
    一來符合實際（before_open 也會收在岸 quote_0845），二來供「有 FX 資料」判定。
    """
    prev = get_previous_trading_day(date)
    conn.execute(
        "INSERT INTO raw_fx (date, currency_pair, close_16) VALUES (?, ?, ?)",
        (prev, pair, close_16),
    )
    conn.execute(
        "INSERT INTO raw_fx (date, currency_pair, quote_0845) VALUES (?, ?, ?)",
        (date, pair, quote_0845),
    )
    if pair == "USD/TWD":
        _seed_twd_offshore(conn, prev, close_16)
        _seed_twd_offshore(conn, date, quote_0845)
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
        # TWD=bullish, KRW=bullish, CNY=None → 2 non-None，但非 3 → sync=0
        assert result["fx_asia_sync"] == 0

    def test_all_null_no_fx_data(self, fx_db):
        """完全無 FX 資料 → 回傳 None。"""
        result = compute_fx_metrics("2026-04-08", fx_db)
        assert result is None

    def test_all_currencies_null_values(self, fx_db):
        """三幣都有 row 但 quote/close（含 TWD 離岸）都是 NULL → 全部 delta None。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", None, None)
        _insert_fx(fx_db, "2026-04-08", "USD/CNY", None, None)
        _insert_fx(fx_db, "2026-04-08", "USD/KRW", None, None)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result is not None  # rows exist, return result with Nones
        assert result["fx_delta_twd"] is None
        assert result["fx_direction"] is None
        assert result["fx_asia_sync"] is None

    def test_boundary_neutral_negative(self, fx_db):
        """delta = -0.05（剛好在門檻上）→ neutral。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.45)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_delta_twd"] == pytest.approx(-0.05)
        assert result["fx_direction"] == "neutral"

    def test_boundary_neutral_positive(self, fx_db):
        """delta = +0.05（剛好在門檻上）→ neutral。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.55)

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_direction"] == "neutral"

    def test_boundary_bearish(self, fx_db):
        """delta 剛好超過門檻（+0.06）→ bearish。"""
        _insert_fx(fx_db, "2026-04-08", "USD/TWD", 31.50, 31.56)

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


class TestFxTwdOffshore:
    """TWD 改用離岸 USDTWD=X 晨對晨（intraday_fx 最後一根）的專屬測試。"""

    def _raw_placeholder(self, conn, date="2026-04-08"):
        """before_open 仍會收在岸 quote_0845；留一筆讓『有 FX 資料』判定通過。"""
        conn.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) "
                     "VALUES (?, 'USD/TWD', 31.40)", (date,))

    def test_morning_to_morning_uses_last_bar(self, fx_db):
        """今晨 − 昨晨，且各取最後一根（最大 ts），亂序插入仍正確。"""
        self._raw_placeholder(fx_db)
        for ts, c in [(100, 31.70), (300, 31.80), (200, 31.75)]:  # 昨晨最後=31.80
            fx_db.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                          "VALUES ('2026-04-07', 'USD/TWD', ?, ?)", (ts, c))
        for ts, c in [(300, 31.92), (100, 31.50), (200, 31.88)]:  # 今晨最後=31.92
            fx_db.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                          "VALUES ('2026-04-08', 'USD/TWD', ?, ?)", (ts, c))
        fx_db.commit()

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_delta_twd"] == pytest.approx(0.12)  # 31.92 - 31.80
        assert result["fx_direction"] == "bearish"

    def test_missing_today_intraday_gives_none(self, fx_db):
        """缺今晨離岸 → delta None、direction None（graceful）。"""
        self._raw_placeholder(fx_db)
        _seed_twd_offshore(fx_db, "2026-04-07", 31.80)  # 只有昨晨
        fx_db.commit()

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_delta_twd"] is None
        assert result["fx_direction"] is None

    def test_missing_prev_intraday_gives_none(self, fx_db):
        """缺昨晨離岸基準 → delta None。"""
        self._raw_placeholder(fx_db)
        _seed_twd_offshore(fx_db, "2026-04-08", 31.85)  # 只有今晨
        fx_db.commit()

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_delta_twd"] is None

    def test_onshore_quote_does_not_drive_twd(self, fx_db):
        """重點回歸：在岸 quote_0845/close_16 不再影響 TWD delta（只看離岸）。"""
        # 在岸給一組會算出 -0.15 的舊資料，但無 intraday → TWD 仍應 None。
        prev = get_previous_trading_day("2026-04-08")
        fx_db.execute("INSERT INTO raw_fx (date, currency_pair, close_16) "
                      "VALUES (?, 'USD/TWD', 31.50)", (prev,))
        fx_db.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) "
                      "VALUES ('2026-04-08', 'USD/TWD', 31.35)")
        fx_db.commit()

        result = compute_fx_metrics("2026-04-08", fx_db)

        assert result["fx_delta_twd"] is None


class TestFxBaselineTimeline:
    """回歸：CNY/KRW delta 必須用前一交易日 close_16，不能用當日（當日早上還沒收）。"""

    def test_delta_uses_prev_day_close(self, fx_db):
        """前一交易日 close_16 + 當日 quote_0845 → 正確算出隔夜 delta（CNY）。"""
        fx_db.execute("INSERT INTO raw_fx (date, currency_pair, close_16) "
                      "VALUES ('2026-04-07', 'USD/CNY', 7.2500)")
        fx_db.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) "
                      "VALUES ('2026-04-08', 'USD/CNY', 7.2400)")
        fx_db.commit()

        result = compute_fx_metrics("2026-04-08", fx_db)
        assert result["fx_delta_cny"] == pytest.approx(-0.01)

    def test_only_today_quote_no_prev_close_gives_none(self, fx_db):
        """只有當日報價、無前一交易日收盤 → delta None（不可退回同列 bug）。"""
        fx_db.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) "
                      "VALUES ('2026-04-08', 'USD/CNY', 7.2400)")
        fx_db.commit()

        result = compute_fx_metrics("2026-04-08", fx_db)
        assert result is not None
        assert result["fx_delta_cny"] is None


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

        # 更新今晨離岸最後一根後重算：delta 變 -0.10
        fx_db.execute(
            "UPDATE intraday_fx SET close = 31.40 "
            "WHERE date = '2026-04-08' AND currency_pair = 'USD/TWD' AND ts = 200"
        )
        fx_db.commit()
        compute_fx_metrics("2026-04-08", fx_db)

        row = fx_db.execute(
            "SELECT fx_delta_twd FROM daily_metrics WHERE date = ?",
            ("2026-04-08",),
        ).fetchone()
        assert row[0] == pytest.approx(-0.10)
