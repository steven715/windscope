"""串流：hub 發佈／訂閱／篩商品／滿了丟；ReplaySource 把 tick 與 1m 回放成一樣的 1m 並落地 bars 表、/api/bars 看得到；
WebSocket /ws/bars 收到 tick／1m／心跳；沒有金鑰的 Shioaji 不啟動；MARKET_SOURCE=none 不接來源。"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from collectors.market_stream import ReplaySource, StreamRunner, make_source
from collectors.shioaji_source import ShioajiSource
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


@pytest.fixture(autouse=True)
def replay_db(tmp_path: Path, monkeypatch) -> str:
    """演練 DB 一律在 tmp（不碰 data/）。"""
    p = str(tmp_path / "replay.db")
    monkeypatch.setattr(settings, "REPLAY_DB_PATH", p)
    return p


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


def test_replay_runner_forwards_1m_rows_unchanged_and_persists_to_the_rehearsal_db(db_path, replay_db, monkeypatch):
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    rows = [r for r in _fixture_rows() if r["instrument_id"] == "TAIFEX:TMF" and r["resolution"] == "1m"]
    hub = StreamHub()
    sid = hub.subscribe()
    runner = StreamRunner(ReplaySource(rows), hub, db_path, instruments=["TAIFEX:TMF"], clock_flush=False)
    assert runner.start() and runner.ticks == 0 and runner.bars == 1        # 1m 列直接推，不拆成假 tick
    runner.stop()
    got = []
    while (r := hub.get(sid, 0.01)) is not None:
        got.append(r)
    kinds = [r["resolution"] for r in got]
    assert kinds == ["1m"]
    bar = got[0]
    src = rows[0]
    assert (bar["ts"], bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == \
           (src["ts"], src["open"], src["high"], src["low"], src["close"], src["volume"])
    assert bar["published_at"] == "2026-06-12T09:21:00+08:00" and bar["session_date"] == "2026-06-12"
    conn = sqlite3.connect(replay_db)
    stored = bars_query(conn, "TAIFEX:TMF", "1m", "2026-06-12", "2026-06-12")
    assert len(stored) == 1 and stored[0]["close"] == src["close"] and stored[0]["source"] == "windscope.bar_builder"
    assert bars_query(conn, "TAIFEX:TMF", "tick", "2026-06-12", "2026-06-12") == []   # 預設不落 tick
    conn.close()
    main = sqlite3.connect(db_path)                                         # 正式 DB 的 bars 表沒有被演練寫到
    assert main.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0
    main.close()
    both = [r for r in _fixture_rows() if r["instrument_id"] == "TAIFEX:TMF" and r["resolution"] in ("tick", "1m")]
    src2 = ReplaySource(both)                                               # 同一分鐘 tick 與 1m 都有 → 只用 tick
    assert [r["resolution"] for r in src2._records] == ["tick"] and len(src2) == 1
    runner2 = StreamRunner(src2, StreamHub(), db_path, instruments=["TAIFEX:TMF"], clock_flush=False)
    runner2.start(); runner2.stop(now="2026-06-12T09:21:00+08:00")        # 以最後一筆之後一分鐘當時鐘 → 收掉 09:20
    assert runner2.ticks == 1 and runner2.bars == 1


def test_runner_drops_unknown_instrument_and_persist_failure_does_not_kill_stream(db_path, caplog, monkeypatch):
    hub = StreamHub()
    runner = StreamRunner(ReplaySource([]), hub, db_path, instruments=["TAIFEX:TMF"], clock_flush=False)
    runner.on_tick("TWSE:9999", "2026-06-12T09:00:00+08:00", 1.0, 1)        # 不在訂閱名單
    runner.on_tick("TAIFEX:TMF", "2026-06-12T14:00:00+08:00", 1.0, 1)       # 時段外
    runner.on_tick("TAIFEX:TMF", "not a time", 1.0, 1)                      # 壞時刻
    assert runner.ticks == 0 and hub.published == 0 and "outside any session" in caplog.text and "unparseable" in caplog.text
    runner.on_tick("TAIFEX:TMF", "2026-06-12T09:00:00.123+08:00", 23000.0, None)   # 毫秒、volume None → 正規化、0
    assert hub.last_record["ts"] == "2026-06-12T09:00:00+08:00" and hub.last_record["volume"] == 0 and runner.ticks == 1
    hub.published = 0; runner.ticks = 0
    monkeypatch.setattr("collectors.market_stream.bars_upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    runner.on_tick("TAIFEX:TMF", "2026-06-12T09:00:01+08:00", 23000.0, 1)
    runner.on_tick("TAIFEX:TMF", "2026-06-12T09:01:01+08:00", 23001.0, 1)   # 收掉 09:00 → 落地失敗 → 只記 ERROR
    assert runner.bars == 1 and hub.published == 3 and "bars persist failed" in caplog.text


def test_make_source_and_shioaji_without_keys_does_not_start(monkeypatch):
    monkeypatch.setattr(settings, "SHIOAJI_API_KEY", "")
    assert ShioajiSource().start(lambda *a: None) is False                   # 沒有金鑰：記 ERROR、不啟動
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


def test_cli_stream_replays_jsonl_into_the_rehearsal_db(db_path, replay_db, tmp_path, monkeypatch, capsys):
    import main as cli
    monkeypatch.setenv("PREMARKET_DB", db_path)
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    rows = [r for r in _fixture_rows() if r["instrument_id"] == "TAIFEX:TMF"]
    p = tmp_path / "bars.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["main.py", "stream", "--jsonl", str(p)])
    cli.main()
    out = capsys.readouterr().out
    assert "1 1m bars, 1 written" in out and "1/1 usable rows" in out and replay_db in out
    conn = sqlite3.connect(replay_db)
    assert len(bars_query(conn, "TAIFEX:TMF", "1m", "2026-06-12", "2026-06-12")) == 1
    conn.close()
    main = sqlite3.connect(db_path)
    assert main.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0
    main.close()


def test_replay_with_delay_runs_to_completion_and_clock_flush_emits_last_bar(db_path, monkeypatch):
    monkeypatch.setattr(settings, "STREAM_FLUSH_SECONDS", 0.05)
    rows = [r for r in _fixture_rows() if r["instrument_id"] == "TAIFEX:TMF" and r["resolution"] == "tick"]
    rows = rows + [{**rows[0], "ts": "2026-06-12T09:20:30+08:00", "published_at": "2026-06-12T09:20:30+08:00"}]
    src = ReplaySource(rows, delay_sec=0.02)
    runner = StreamRunner(src, StreamHub(), db_path, instruments=["TAIFEX:TMF"])      # clock_flush 預設開
    assert runner.start()
    src.wait(5)
    import time
    time.sleep(0.2)                                                        # 時鐘（牆鐘 ≥ 09:21）把 09:20 收掉
    assert src.replayed == 2 and runner.ticks == 2 and runner.bars == 1
    runner.stop()
    assert runner.bars == 1                                                # stop 不會再發一次


def test_start_market_stream_never_breaks_server_boot(db_path, monkeypatch, caplog):
    from collectors.market_stream import start_market_stream
    hub = StreamHub()
    monkeypatch.setattr(settings, "MARKET_SOURCE", "bloomberg")
    assert start_market_stream(hub, db_path) is None and "unknown MARKET_SOURCE" in caplog.text
    monkeypatch.setattr(settings, "MARKET_SOURCE", "replay")
    monkeypatch.setattr(settings, "STREAM_REPLAY_JSONL", "")
    assert start_market_stream(hub, db_path) is None and "needs STREAM_REPLAY_JSONL" in caplog.text
    monkeypatch.setattr(settings, "MARKET_SOURCE", "shioaji")
    monkeypatch.setattr(settings, "SHIOAJI_API_KEY", "")                     # 沒有金鑰：記 ERROR、server 照常
    assert start_market_stream(hub, db_path) is None and "SHIOAJI_API_KEY" in caplog.text
    monkeypatch.setattr(settings, "MARKET_SOURCE", "none")
    assert start_market_stream(hub, db_path) is None


def _tmf_1m() -> dict:
    return next(r for r in _fixture_rows() if r["instrument_id"] == "TAIFEX:TMF" and r["resolution"] == "1m")


def _tables(path: str) -> set[str]:
    c = sqlite3.connect(path)
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        c.close()


def test_replay_writes_are_refused_on_the_production_db(db_path, monkeypatch):
    """寫入口的保險：replay=True 寫正式 DB 一律拒絕，真來源照常寫。"""
    from db.bars import bars_upsert
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    with pytest.raises(ValueError, match="production DB"):
        bars_upsert(conn, [_tmf_1m()], replay=True)
    assert bars_upsert(conn, [_tmf_1m()]) == 1
    conn.rollback()
    conn.close()


def test_misconfigured_rehearsal_db_disables_persistence_once_without_touching_the_production_schema(tmp_path, monkeypatch, caplog):
    """REPLAY_DB 設成正式 DB（設定錯）：建 runner 時就關掉落地、只記一次 ERROR，串流照推，正式 DB 不建表也不寫。"""
    prod = str(tmp_path / "prod.db")
    sqlite3.connect(prod).close()                                          # 空的正式 DB（沒有表）
    monkeypatch.setattr(settings, "DB_PATH", prod)
    monkeypatch.setattr(settings, "REPLAY_DB_PATH", prod)
    monkeypatch.setattr(settings, "PRODUCTION_DB_PATH", prod)
    hub = StreamHub()
    q = hub.subscribe()
    rows = [_tmf_1m(), {**_tmf_1m(), "ts": _tmf_1m()["ts"][:14] + "59:00+08:00"}]
    runner = StreamRunner(ReplaySource(rows), hub, None, instruments=["TAIFEX:TMF"], clock_flush=False)
    assert runner.start() and runner.bars == 2 and runner.persisted == 0
    runner.stop()
    assert hub.get(q, 0.01) is not None and hub.get(q, 0.01) is not None   # 兩根都推出去了
    assert caplog.text.count("will NOT be persisted") == 1 and "persist failed" not in caplog.text
    assert _tables(prod) == set()


def test_rehearsal_server_mode_writes_its_own_db(tmp_path, monkeypatch):
    """演練 server：PREMARKET_DB＝REPLAY_DB＝演練檔，演練照常落地；放真資料的檔（PRODUCTION_DB）仍然拒寫。"""
    from db.bars import bars_upsert
    rehearsal, prod = str(tmp_path / "replay.db"), str(tmp_path / "premarket.db")
    sqlite3.connect(prod).close()
    monkeypatch.setattr(settings, "DB_PATH", rehearsal)
    monkeypatch.setattr(settings, "REPLAY_DB_PATH", rehearsal)
    monkeypatch.setattr(settings, "PRODUCTION_DB_PATH", prod)
    runner = StreamRunner(ReplaySource([_tmf_1m()]), StreamHub(), rehearsal, instruments=["TAIFEX:TMF"], clock_flush=False)
    assert runner.start() and runner.persisted == 1
    runner.stop()
    conn = sqlite3.connect(prod)
    create_all_tables(conn)
    with pytest.raises(ValueError, match="production DB"):
        bars_upsert(conn, [_tmf_1m()], replay=True)
    conn.close()


def test_hard_link_to_the_production_db_is_still_refused(db_path, tmp_path, monkeypatch):
    import os
    from db.bars import bars_upsert
    link = str(tmp_path / "alias.db")
    os.link(db_path, link)
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    monkeypatch.setattr(settings, "PRODUCTION_DB_PATH", db_path)          # 硬連結到放真資料的檔
    monkeypatch.setattr(settings, "REPLAY_DB_PATH", link)
    conn = sqlite3.connect(link)
    with pytest.raises(ValueError, match="production DB"):
        bars_upsert(conn, [_tmf_1m()], replay=True)
    conn.close()
    runner = StreamRunner(ReplaySource([_tmf_1m()]), StreamHub(), db_path, instruments=["TAIFEX:TMF"], clock_flush=False)
    assert runner.start() and runner.persisted == 0
    runner.stop()
    main = sqlite3.connect(db_path)
    assert main.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 0
    main.close()


def test_rehearsal_db_directory_is_created(tmp_path, monkeypatch, db_path):
    p = str(tmp_path / "nested" / "dir" / "replay.db")
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    monkeypatch.setattr(settings, "REPLAY_DB_PATH", p)
    runner = StreamRunner(ReplaySource([_tmf_1m()]), StreamHub(), db_path, instruments=["TAIFEX:TMF"], clock_flush=False)
    assert runner.start() and runner.persisted == 1
    runner.stop()


def test_non_replay_sources_still_persist_to_the_main_db(db_path, monkeypatch):
    """真來源（非演練）照舊寫正式 DB：餵一筆 1m 進 on_bar，正式 DB 看得到。"""
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    row = _tmf_1m()

    class Live:
        name = "shioaji"

        def start(self, on_tick, on_bar):
            on_bar(dict(row))
            return True

        def stop(self):
            pass
    runner = StreamRunner(Live(), StreamHub(), db_path, instruments=["TAIFEX:TMF"], clock_flush=False)
    assert runner.persist_db == db_path and not runner.replay
    assert runner.start() and runner.persisted == 1
    runner.stop()
    main = sqlite3.connect(db_path)
    assert len(bars_query(main, "TAIFEX:TMF", "1m", "2026-06-12", "2026-06-12")) == 1
    main.close()
