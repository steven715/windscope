"""純導出層測試：recompute_date 從 raw 導出指標與訊號，且冪等、可重現。"""

import sqlite3

from db.schema import create_all_tables
from integration.recompute import recompute_date


def _setup_raw(conn):
    """塞入會導出『偏多』訊號的原始事實：匯率升值 + 期貨正價差。"""
    # raw_fx：留在岸 row（before_open 也會收）；TWD delta 走離岸晨對晨。
    conn.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) "
                 "VALUES ('2026-06-15', 'USD/TWD', 31.35)")
    # 離岸晨對晨：昨晨 31.50 → 今晨 31.35（TWD 升值 0.15 → bullish）。
    for d, close in [("2026-06-12", 31.50), ("2026-06-15", 31.35)]:
        conn.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                     "VALUES (?, 'USD/TWD', 200, ?)", (d, close))
    # raw_futures：前一交易日現貨 + 當日夜盤（spread +150 → bullish）
    conn.execute("INSERT INTO raw_futures (date, spot_close) "
                 "VALUES ('2026-06-12', 20000)")
    conn.execute("INSERT INTO raw_futures (date, night_close, night_volume) "
                 "VALUES ('2026-06-15', 20150, 40000)")
    conn.commit()


def test_recompute_derives_metrics_and_signal():
    """從 raw 導出 daily_metrics 與 signals。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    _setup_raw(conn)

    result = recompute_date("2026-06-15", conn)

    assert result["fx_metrics"] is True
    assert result["futures_metrics"] is True
    assert result["signal"] is not None

    row = conn.execute(
        "SELECT fx_direction, futures_spread FROM daily_metrics WHERE date = ?",
        ("2026-06-15",),
    ).fetchone()
    assert row[0] == "bullish"
    assert row[1] == 150.0

    sig = conn.execute(
        "SELECT direction FROM signals WHERE date = ?", ("2026-06-15",)
    ).fetchone()
    assert sig is not None and sig[0] == "bullish"


def test_recompute_is_idempotent():
    """同一輸入重跑得同一輸出，且衍生資料是覆寫非堆疊。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    _setup_raw(conn)

    r1 = recompute_date("2026-06-15", conn)
    r2 = recompute_date("2026-06-15", conn)

    assert r1["signal"]["direction"] == r2["signal"]["direction"]
    assert r1["signal"]["confidence"] == r2["signal"]["confidence"]

    n = conn.execute(
        "SELECT COUNT(*) FROM daily_metrics WHERE date = ?", ("2026-06-15",)
    ).fetchone()[0]
    assert n == 1


def test_recompute_no_raw_returns_empty_signal():
    """無 raw 資料 → 不報錯，signal 為 None。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)

    result = recompute_date("2026-06-15", conn)

    assert result["fx_metrics"] is False
    assert result["futures_metrics"] is False
    assert result["signal"] is None
