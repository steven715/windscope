"""時段歸屬（夜盤歸次一交易日、凌晨、時段外）與 1 分 K 合成（只在分鐘結束發出、缺分鐘不補、亂序丟棄、flush、時段切換）。"""

from unittest.mock import patch

import pytest

from integration.bar_builder import BarBuilder, tick_record
from integration.market_sessions import attribute, session_at

FUT = {"regular": {"open": "08:45", "close": "13:45"}, "night": {"open": "15:00", "close": "05:00"}}
STK = {"regular": {"open": "09:00", "close": "13:30"}}


@pytest.fixture(autouse=True)
def _no_holidays():
    with patch("integration.market_sessions.is_trading_day", side_effect=lambda d: __import__("datetime").date.fromisoformat(d).weekday() < 5), \
         patch("integration.market_sessions.get_next_trading_day", side_effect=_next_weekday):
        yield


def _next_weekday(d: str) -> str:
    from datetime import date, timedelta
    x = date.fromisoformat(d) + timedelta(days=1)
    while x.weekday() >= 5:
        x += timedelta(days=1)
    return x.isoformat()


def test_session_at_and_attribution():
    assert session_at(FUT, "09:00") == "regular" and session_at(FUT, "23:00") == "night" and session_at(FUT, "04:59") == "night"
    assert session_at(FUT, "14:30") is None and session_at(STK, "08:59") is None
    assert attribute(FUT, "2026-06-12T09:00:00+08:00") == ("regular", "2026-06-12")
    assert attribute(FUT, "2026-06-12T15:00:00+08:00") == ("night", "2026-06-15")          # 週五夜盤 → 下週一
    assert attribute(FUT, "2026-06-13T02:00:00+08:00") == ("night", "2026-06-15")          # 週六凌晨 → 下週一
    assert attribute(FUT, "2026-06-11T23:30:00+08:00") == ("night", "2026-06-12")
    assert attribute(FUT, "2026-06-12T04:59:00+08:00") == ("night", "2026-06-12")          # 週五凌晨：當天是交易日
    assert attribute(FUT, "2026-06-12T14:00:00+08:00") == (None, None)
    assert attribute(STK, "2026-06-12T13:30:00+08:00") == ("regular", "2026-06-12")


def test_tick_record_shape_and_out_of_session():
    t = tick_record("TAIFEX:TMF", "2026-06-12T09:20:03+08:00", 23015, 3, FUT, "2026-06-12T09:20:03+08:00", "broker.replay")
    assert t["resolution"] == "tick" and t["open"] is None and t["close"] == 23015.0 and t["published_at"] == t["ts"]
    assert t["session"] == "regular" and t["session_date"] == "2026-06-12" and t["volume"] == 3
    assert tick_record("TAIFEX:TMF", "2026-06-12T14:00:00+08:00", 1, 1, FUT, "x", "s") is None


def _tick(ts: str, px: float, vol: int = 1, sess: str = "regular", d: str = "2026-06-12") -> dict:
    return {"instrument_id": "TAIFEX:TMF", "resolution": "tick", "session": sess, "session_date": d, "ts": ts,
            "close": px, "volume": vol, "published_at": ts}


def test_builder_emits_only_completed_minutes_and_skips_empty_minutes():
    b = BarBuilder("TAIFEX:TMF", FUT)
    assert b.on_tick(_tick("2026-06-12T09:20:03+08:00", 23010, 2), "2026-06-12T09:20:03+08:00") == []
    assert b.on_tick(_tick("2026-06-12T09:20:40+08:00", 23022, 5), "2026-06-12T09:20:40+08:00") == []
    assert b.on_tick(_tick("2026-06-12T09:20:59+08:00", 23008, 1), "2026-06-12T09:20:59+08:00") == []
    out = b.on_tick(_tick("2026-06-12T09:23:01+08:00", 23030, 1), "2026-06-12T09:23:01+08:00")   # 09:21、09:22 沒成交
    assert len(out) == 1
    bar = out[0]
    assert (bar["ts"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == ("2026-06-12T09:20:00+08:00", 23010, 23022, 23008, 23008, 8)
    assert bar["published_at"] == "2026-06-12T09:21:00+08:00" and bar["ingested_at"] >= bar["published_at"]
    assert bar["resolution"] == "1m" and bar["source"] == "windscope.bar_builder"
    assert b.on_tick(_tick("2026-06-12T09:22:30+08:00", 1), "x") == []                      # 亂序：丟掉
    assert b.flush("2026-06-12T09:23:30+08:00") == []                                          # 09:23 還沒結束
    flushed = b.flush("2026-06-12T09:24:00+08:00")
    assert len(flushed) == 1 and flushed[0]["ts"] == "2026-06-12T09:23:00+08:00" and flushed[0]["close"] == 23030
    assert b.flush("2026-06-12T09:30:00+08:00") == []


def test_builder_separates_sessions_and_midnight():
    b = BarBuilder("TAIFEX:TMF", FUT)
    b.on_tick(_tick("2026-06-12T13:44:50+08:00", 23000), "x")
    out = b.on_tick(_tick("2026-06-12T15:00:05+08:00", 23005, sess="night", d="2026-06-15"), "x")
    assert out[0]["session"] == "regular" and out[0]["ts"] == "2026-06-12T13:44:00+08:00"
    out = b.on_tick(_tick("2026-06-12T23:59:30+08:00", 23007, sess="night", d="2026-06-15"), "x")
    assert out[0]["ts"] == "2026-06-12T15:00:00+08:00" and out[0]["session"] == "night" and out[0]["session_date"] == "2026-06-15"
    out = b.flush("2026-06-13T00:00:00+08:00")
    assert out[0]["ts"] == "2026-06-12T23:59:00+08:00" and out[0]["published_at"] == "2026-06-13T00:00:00+08:00"


def test_late_tick_after_flush_is_dropped_not_reopened():
    b = BarBuilder("TAIFEX:TMF", FUT)
    b.on_tick(_tick("2026-06-12T09:23:10+08:00", 100, 5), "x")
    assert b.flush("2026-06-12T09:24:00+08:00")[0]["volume"] == 5          # 時鐘收掉 09:23
    assert b.on_tick(_tick("2026-06-12T09:23:58+08:00", 101, 1), "x") == []   # 交易所時間戳晚到：丟，不重開
    out = b.on_tick(_tick("2026-06-12T09:24:30+08:00", 102, 1), "x")
    assert out == []                                                       # 09:23 不會再發第二次
    assert b.flush("2026-06-12T09:25:00+08:00")[0]["ts"] == "2026-06-12T09:24:00+08:00"
