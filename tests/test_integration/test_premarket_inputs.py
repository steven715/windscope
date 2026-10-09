"""盤前輸入快照測試：基準日、fallback 與出處、缺料、票與解讀／摘要一致。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from integration.explain import build_explain
from integration.futures_metrics import compute_futures_metrics
from integration.premarket_inputs import (
    SPOT_FROM_FUTURES,
    SPOT_FROM_INDEX,
    load_premarket_inputs,
)
from integration.summary import generate_daily_summary

# 2026-06-17(三) 的前一交易日 06-16(二)、再前一 06-15(一)
DATE, PREV, PREV2 = "2026-06-17", "2026-06-16", "2026-06-15"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    create_all_tables(c)
    yield c
    c.close()


def _seed(c, prev_spot=45000.0, index_close=None):
    """前日現貨（可 NULL 模擬 FMTQIK 缺料）＋指數收盤＋當日夜盤＋匯率。"""
    c.execute("INSERT INTO raw_futures (date, oi_net_foreign) VALUES (?, -30000)", (PREV2,))
    c.execute(
        "INSERT INTO raw_futures (date, spot_close, night_volume, oi_net_foreign, sp500_close) "
        "VALUES (?, ?, 1000, -25000, 6000)", (PREV, prev_spot))
    if index_close is not None:
        c.execute("INSERT INTO raw_index (date, close) VALUES (?, ?)", (PREV, index_close))
    c.execute(
        "INSERT INTO raw_futures (date, night_close, night_volume, ex_dividend_points, sp500_close) "
        "VALUES (?, 45150, 2000, 10, 6060)", (DATE,))
    for pair, close16, q in [("USD/CNY", 7.10, 7.08), ("USD/JPY", 150.0, 149.0)]:
        c.execute("INSERT INTO raw_fx (date, currency_pair, close_16) VALUES (?, ?, ?)",
                  (PREV, pair, close16))
        c.execute("INSERT INTO raw_fx (date, currency_pair, quote_0845) VALUES (?, ?, ?)",
                  (DATE, pair, q))
    for d, close in [(PREV, 31.60), (DATE, 31.50)]:
        c.execute("INSERT INTO intraday_fx (date, currency_pair, ts, close) "
                  "VALUES (?, 'USD/TWD', 1, ?)", (d, close))
    c.commit()


def test_happy_path(conn):
    _seed(conn)
    inp = load_premarket_inputs(DATE, conn)
    assert inp.prev_day == PREV
    assert inp.has_futures_row and inp.night_close == 45150
    assert (inp.prev_spot, inp.prev_spot_source) == (45000.0, SPOT_FROM_FUTURES)
    assert inp.recent_night_volumes == (1000,)
    assert inp.night_volume_avg == 1000
    assert (inp.oi_prev, inp.oi_prev2) == (-25000, -30000)
    assert (inp.sp500_today, inp.sp500_prev) == (6060, 6000)
    assert (inp.twd_offshore_today, inp.twd_offshore_prev) == (31.50, 31.60)
    assert inp.fx_quote_0845["USD/JPY"] == 149.0
    assert inp.fx_prev_close_16["USD/CNY"] == 7.10
    assert "USD/KRW" not in inp.fx_quote_0845  # 當日無 row → 不列入
    with pytest.raises(TypeError):
        inp.fx_quote_0845["USD/KRW"] = 1.0  # 快照唯讀


def test_prev_spot_falls_back_to_index_with_source(conn):
    _seed(conn, prev_spot=None, index_close=44990.0)
    inp = load_premarket_inputs(DATE, conn)
    assert (inp.prev_spot, inp.prev_spot_source) == (44990.0, SPOT_FROM_INDEX)


def test_empty_db_all_none(conn):
    inp = load_premarket_inputs(DATE, conn)
    assert inp.prev_day == PREV
    assert not inp.has_futures_row
    assert inp.prev_spot is None and inp.prev_spot_source is None
    assert inp.night_volume_avg is None
    assert inp.fx_quote_0845 == {}


def test_explain_and_summary_use_fallback_base_like_vote(conn):
    """回歸：FMTQIK 缺料日，票有 fallback 基準 → 解讀與摘要也要顯示同一基準（舊版顯示資料不可用）。"""
    _seed(conn, prev_spot=None, index_close=44990.0)
    m = compute_futures_metrics(DATE, conn)
    assert m["futures_spread"] == 160.0

    spread_row = next(r for r in build_explain(DATE, conn) if r["dim"] == "期貨價差")
    assert "指數收盤補" in spread_row["raw"] and "44990" in spread_row["raw"]

    text = generate_daily_summary(DATE, conn)
    assert "前日現貨  44990" in text


def test_explain_twd_row_no_crash_when_delta_not_computed(conn):
    """離岸晨間兩值都有但 daily_metrics.fx_delta_twd 尚未算（NULL）→ 不拋 TypeError。"""
    _seed(conn)
    conn.execute("INSERT INTO daily_metrics (date, fx_direction) VALUES (?, NULL)", (DATE,))
    conn.commit()
    twd = next(r for r in build_explain(DATE, conn) if r["dim"] == "匯率（台幣）")
    assert twd["raw"] == "資料不足"
