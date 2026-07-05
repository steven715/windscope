"""還原股價（back_adjust）測試：分割/大除權缺口偵測 + 配息因子回推。"""

from integration.price_adjust import adjust_note, back_adjust, back_adjust_with_events


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
