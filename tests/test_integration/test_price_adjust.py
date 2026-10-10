"""還原股價（back_adjust）測試：分割/大除權缺口偵測 + 配息因子回推。"""

import sqlite3

import pytest

from db.schema import create_all_tables
from integration.price_adjust import (
    adjust_note,
    back_adjust,
    back_adjust_with_events,
    load_adjusted_bars,
    load_bars,
)


def _bars(closes: list[float]) -> list[dict]:
    return [{"date": f"d{i}", "open": c, "high": c * 1.01, "low": c * 0.99,
             "close": c, "volume": 1} for i, c in enumerate(closes)]


def test_empty():
    assert back_adjust([]) == []


def test_no_gaps_passthrough_and_no_mutation():
    bars = _bars([100, 101, 102, 103])
    out = back_adjust(bars)
    assert [round(b["close"], 4) for b in out] == [100, 101, 102, 103]
    assert bars[0]["close"] == 100          # 不改動原物件


def test_split_gap_removed():
    """1:4 分割（202→50，-75% > 11%）→ 分割前縮到分割後尺度，缺口消失。"""
    out = back_adjust(_bars([200, 202, 50, 51, 52]))
    closes = [round(b["close"], 2) for b in out]
    assert closes[-1] == 52                  # 最新不變
    assert abs(closes[1] - closes[2]) < 1.5  # 相鄰缺口消失（連續）
    assert closes[0] < 60                    # 分割前價已縮小
    # OHLC 一起縮、結構仍在
    assert out[0]["high"] > out[0]["close"] > out[0]["low"]


def test_limit_move_not_adjusted():
    """單日 +10%（漲停）在 ±10% 限制內，不算公司行為、不還原。"""
    out = back_adjust(_bars([100, 110, 111]))
    assert [round(b["close"], 2) for b in out] == [100, 110, 111]


def test_dividend_factor_applied():
    """配息（<10% 看不出）用 div_events 的 factor 套在 ex-date 之前。"""
    out = back_adjust(_bars([100, 100, 99, 99]),
                      [{"ex_date": "d2", "factor": 0.99}])
    closes = [round(b["close"], 2) for b in out]
    assert closes == [99.0, 99.0, 99.0, 99.0]   # d2 前乘 0.99，d2 起不變


def test_dividend_skipped_when_price_gap_present():
    """該日已是價格缺口（分割）→ 用實際價格比例，不重複套 div factor。"""
    out = back_adjust(_bars([200, 50, 51]), [{"ex_date": "d1", "factor": 0.99}])
    assert round(out[0]["close"], 2) == round(200 * (50 / 200), 2)  # 用 0.25 非 0.99


def test_multiple_events_cumulative():
    """分割 + 之後配息 → 分割前的價格吃到兩個 factor 的累積。"""
    # d1 分割 100→40（factor .4）；d3 配息 factor .95
    out = back_adjust(_bars([100, 40, 41, 41]), [{"ex_date": "d3", "factor": 0.95}])
    # d0 在 d1 與 d3 之前 → 100 * .4 * .95 = 38.0
    assert round(out[0]["close"], 2) == 38.0
    assert round(out[-1]["close"], 2) == 41.0


def test_consecutive_gaps_compound():
    """連續兩根都是缺口 → 各自 factor 對更早的日子累乘。"""
    # d1: 100→40 (.4)；d2: 40→10 (.25)
    out = back_adjust(_bars([100, 40, 10, 10]))
    # d0 在 d1、d2 之前 → 100*.4*.25 = 10；d1 在 d2 之前 → 40*.25 = 10
    assert round(out[0]["close"], 2) == 10.0
    assert round(out[1]["close"], 2) == 10.0
    assert out[2]["close"] == 10 and out[3]["close"] == 10


def test_zero_close_does_not_zero_history():
    """某日 close=0（異常）不應把之前全部歸零（cur>0 才視為缺口）。"""
    out = back_adjust(_bars([100, 0, 50, 51]))
    assert out[0]["close"] == 100          # 未被 factor=0 歸零
    assert out[3]["close"] == 51


def test_events_reported():
    """back_adjust_with_events 回報 gap 與 div 事件。"""
    _, events = back_adjust_with_events(_bars([200, 50, 51]),
                                        [{"ex_date": "d2", "factor": 0.99}])
    kinds = [(e["kind"], e["date"]) for e in events]
    assert ("gap", "d1") in kinds        # 200→50 分割
    assert ("div", "d2") in kinds        # 配息


def test_adjust_note_only_flags_gaps():
    """adjust_note 只提醒公司行為缺口(gap)，配息(div)不列入（低風險例行）。"""
    assert adjust_note([]) is None
    assert adjust_note([{"kind": "div", "date": "d2", "factor": 0.99}]) is None
    note = adjust_note([{"kind": "gap", "date": "2025-06-18", "factor": 0.25}])
    assert note is not None and "2025-06-18" in note and "1 次" in note


# ── 讀取入口：load_bars / load_adjusted_bars ──────────────────────

@pytest.fixture
def split_db():
    """2330：06-09 收 990、06-10 收 1000 → 06-11 一拆二收 500（缺口）→ 06-12 收 510；
    另一筆 OHLC 缺值列。"""
    c = sqlite3.connect(":memory:")
    create_all_tables(c)
    for d, px in (("2026-06-09", 990.0), ("2026-06-10", 1000.0), ("2026-06-11", 500.0),
                  ("2026-06-12", 510.0)):
        c.execute("INSERT INTO raw_stock_daily (stock_id, date, open, high, low, close, volume) "
                  "VALUES ('2330', ?, ?, ?, ?, ?, 100)", (d, px, px, px, px))
    c.execute("INSERT INTO raw_stock_daily (stock_id, date, close) VALUES ('2330', '2026-06-13', 515)")
    c.commit()
    yield c
    c.close()


def test_load_bars_raw_skips_incomplete_rows(split_db):
    bars = load_bars("2330", split_db)
    assert [b["date"] for b in bars] == ["2026-06-09", "2026-06-10", "2026-06-11", "2026-06-12"]
    assert bars[1]["close"] == 1000.0 and bars[1]["volume"] == 100


def test_load_adjusted_bars_back_adjusts_split(split_db):
    bars, events = load_adjusted_bars("2330", split_db)
    assert bars[1]["close"] == pytest.approx(500.0)       # 拆分前價格縮到同一尺度
    assert bars[-1]["close"] == 510.0                      # 最新價不動
    assert [e["kind"] for e in events] == ["gap"]


def test_load_adjusted_bars_asof_no_lookahead(split_db):
    """asof 在拆分前：看不到之後的缺口，價格不被回推（回測不看未來）。"""
    bars, events = load_adjusted_bars("2330", split_db, asof="2026-06-10")
    assert [b["close"] for b in bars] == [990.0, 1000.0] and events == []


def test_load_adjusted_bars_applies_dividend_factor(split_db):
    """配息因子（stock_dividends）有被讀進來套用：除息日之前的價格按比例回推。"""
    split_db.execute("INSERT INTO stock_dividends (stock_id, ex_date, factor) "
                     "VALUES ('2330', '2026-06-12', 0.98)")
    split_db.commit()
    bars, events = load_adjusted_bars("2330", split_db)
    assert sorted(e["kind"] for e in events) == ["div", "gap"]
    assert bars[2]["close"] == pytest.approx(500.0 * 0.98)


def test_load_adjusted_bars_empty():
    c = sqlite3.connect(":memory:")
    create_all_tables(c)
    assert load_adjusted_bars("9999", c) == ([], [])
    c.close()
