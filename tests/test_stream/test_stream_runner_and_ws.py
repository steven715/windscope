"""串流：hub 發佈／訂閱／篩商品／滿了丟；ReplaySource 把 tick 與 1m 回放成一樣的 1m 並落地 bars 表、/api/bars 看得到；
WebSocket /ws/bars 收到 tick／1m／心跳；Shioaji 是 stub；MARKET_SOURCE=none 不接來源。"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collectors.market_stream import ReplaySource, ShioajiSource, StreamRunner, make_source
from config import settings
from db.bars import bars_query
from db.instruments import instruments_seed
from db.schema import create_all_tables
from server.app import create_app
from server.stream import StreamHub

FIX = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "fixtures" / "bars.jsonl"


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    create_all_tables(conn)
    instruments_seed(conn)
    conn.close()
    return str(p)


def _fixture_rows() -> list[dict]:
    return [json.loads(ln) for ln in FIX.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_hub_pubsub_filter_and_overflow():
    hub = StreamHub()
    a, b = hub.subscribe(), hub.subscribe({"TWSE:2330"})
    hub.publish({"instrument_id": "TAIFEX:TMF", "x": 1})
    assert hub.get(a, 0.1) == {"instrument_id": "TAIFEX:TMF", "x": 1} and hub.get(b, 0.05) is None
    hub.unsubscribe(b)
    assert hub.subscribers == 1 and hub.get(b, 0.01) is None and hub.published == 1
    for _ in range(10_001):
        hub.publish({"instrument_id": "TAIFEX:TMF"})
    assert hub.dropped == 1                                                 # 慢訂閱者滿了：丟，不擋來源


def test_replay_runner_builds_identical_1m_and_persists(db_path):
    rows = [r for r in _fixture_rows() if r["instrument_id"] == "TAIFEX:TMF" and r["resolution"] == "1m"]
    hub = StreamHub()
    sid = hub.subscribe()
    runner = StreamRunner(ReplaySource(rows), hub, db_path, instruments=["TAIFEX:TMF"])
    assert runner.start() and runner.ticks == 4                             # 1 根 1m 拆成開高低收四筆
    runner.stop()
    got = []
    while (r := hub.get(sid, 0.01)) is not None:
        got.append(r)
    kinds = [r["resolution"] for r in got]
    assert kinds.count("tick") == 4 and kinds.count("1m") == 1
    bar = next(r for r in got if r["resolution"] == "1m")
    src = rows[0]
    assert (bar["ts"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == \
           (src["ts"], src["open"], src["high"], src["low"], src["close"], src["volume"])
    assert bar["published_at"] == "2026-06-12T09:21:00+08:00" and bar["session_date"] == "2026-06-12"
    conn = sqlite3.connect(db_path)
    stored = bars_query(conn, "TAIFEX:TMF", "1m", "2026-06-12", "2026-06-12")
    assert len(stored) == 1 and stored[0]["close"] == src["close"] and stored[0]["source"] == "windscope.bar_builder"
    assert bars_query(conn, "TAIFEX:TMF", "tick", "2026-06-12", "2026-06-12") == []   # 預設不落 tick
    conn.close()
    both = [r for r in _fixture_rows() if r["instrument_id"] == "TAIFEX:TMF" and r["resolution"] in ("tick", "1m")]
    src2 = ReplaySource(both)                                               # 同一分鐘 tick 與 1m 都有 → 只用 tick
    assert [r["resolution"] for r in src2._records] == ["tick"]


def test_runner_drops_unknown_instrument_and_persist_failure_does_not_kill_stream(db_path, caplog, monkeypatch):
    hub = StreamHub()
    runner = StreamRunner(ReplaySource([]), hub, db_path, instruments=["TAIFEX:TMF"])
    runner.on_tick("TWSE:9999", "2026-06-12T09:00:00+08:00", 1.0, 1)        # 不在訂閱名單
    runner.on_tick("TAIFEX:TMF", "2026-06-12T14:00:00+08:00", 1.0, 1)       # 時段外
    assert runner.ticks == 0 and hub.published == 0 and "outside any session" in caplog.text
    monkeypatch.setattr("collectors.market_stream.bars_upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    runner.on_tick("TAIFEX:TMF", "2026-06-12T09:00:01+08:00", 23000.0, 1)
    runner.on_tick("TAIFEX:TMF", "2026-06-12T09:01:01+08:00", 23001.0, 1)   # 收掉 09:00 → 落地失敗 → 只記 ERROR
    assert runner.bars == 1 and hub.published == 3 and "bars persist failed" in caplog.text


def test_shioaji_source_is_a_stub_and_make_source():
    assert ShioajiSource().start(lambda *a: None) is False
    assert make_source("none") is None and isinstance(make_source("shioaji"), ShioajiSource)
    with pytest.raises(ValueError):
        make_source("bloomberg")


def test_ws_bars_streams_records_and_heartbeats(db_path, monkeypatch):
    monkeypatch.setattr(settings, "STREAM_HEARTBEAT_SECONDS", 0.2)
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        assert app.state.stream is None                                      # MARKET_SOURCE=none：不接來源
        hub = app.state.hub
        with client.websocket_connect("/ws/bars?instruments=TAIFEX:TMF") as ws:
            first = ws.receive_json()
            assert "heartbeat" in first and first["heartbeat"].endswith("+08:00")   # 閒置 → 心跳
            hub.publish({"instrument_id": "TWSE:2330", "resolution": "tick"})       # 沒訂這個商品
            hub.publish({"instrument_id": "TAIFEX:TMF", "resolution": "tick", "close": 23015.0})
            got = ws.receive_json()
            while "heartbeat" in got:
                got = ws.receive_json()
            assert got == {"instrument_id": "TAIFEX:TMF", "resolution": "tick", "close": 23015.0}
        assert hub.subscribers == 0                                          # 斷線後退訂


def test_cli_stream_replays_jsonl_into_bars(db_path, tmp_path, monkeypatch, capsys):
    import main as cli
    monkeypatch.setenv("PREMARKET_DB", db_path)
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    rows = [r for r in _fixture_rows() if r["instrument_id"] == "TAIFEX:TMF"]
    p = tmp_path / "bars.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["main.py", "stream", "--jsonl", str(p)])
    cli.main()
    out = capsys.readouterr().out
    assert "1 1m bars written" in out
    conn = sqlite3.connect(db_path)
    assert len(bars_query(conn, "TAIFEX:TMF", "1m", "2026-06-12", "2026-06-12")) == 1
    conn.close()
