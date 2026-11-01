"""契約 API：instruments／bars／targets（含 lock）／events（GET＋POST）／account-snapshots。"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from db.instruments import instruments_seed
from db.schema import create_all_tables
from db.targets import target_put
from server.app import create_app

FIX = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "fixtures"


def _fixture(name: str) -> list[dict]:
    return [json.loads(l) for l in (FIX / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture
def client(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    instruments_seed(conn)
    conn.execute("INSERT INTO raw_index (date, open, high, low, close, collected_at) "
                 "VALUES ('2026-06-12', 22890, 23010, 22850, 22980, '2026-06-12T14:30:12')")
    for t in _fixture("targets"):
        target_put(conn, {**t, "locked_at": None})
    conn.commit(); conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False))


def test_instruments(client):
    body = client.get("/api/instruments").json()
    assert body["schema_version"] == "0.1" and body["count"] >= 6
    assert all(i["tradable"] for i in client.get("/api/instruments?tradable_only=true").json()["records"])


def test_bars_from_legacy_view(client):
    body = client.get("/api/bars?instrument_id=TWSE:TAIEX").json()
    assert body["schema_version"] == "0.2" and body["count"] == 1
    assert body["records"][0]["published_at"] == "2026-06-12T13:30:00+08:00"
    assert client.get("/api/bars?instrument_id=TWSE:TAIEX&date_from=2026-07-01").json()["count"] == 0


def test_targets_list_and_lock(client):
    body = client.get("/api/targets?date=2026-06-12").json()
    assert body["schema_version"] == "0.2" and body["count"] == 1
    tid = body["records"][0]["target_id"]
    assert body["records"][0]["locked_at"] is None
    r = client.post(f"/api/targets/{tid}/lock")
    assert r.status_code == 200 and r.json()["locked_at"].endswith("+08:00")
    first = r.json()["locked_at"]
    assert client.post(f"/api/targets/{tid}/lock").json()["locked_at"] == first      # 再鎖不變
    assert client.get("/api/targets?date=2026-06-12").json()["records"][0]["locked_at"] == first
    assert client.post("/api/targets/nope/lock").status_code == 404
    assert client.get("/api/targets?strategy_id=trend_signal").json()["count"] == 2


def test_events_post_get_idempotent(client):
    ev, sn = _fixture("events"), _fixture("account_snapshots")
    r = client.post("/api/events", json={"events": ev, "account_snapshots": sn})
    assert r.status_code == 200
    assert r.json()["events"]["inserted"] == len(ev) and r.json()["account_snapshots"]["inserted"] == 2
    r2 = client.post("/api/events", json={"events": ev + [{"event_id": "bad"}]})
    assert r2.json()["events"] == {**r2.json()["events"], "inserted": 0, "duplicates": len(ev), "invalid": 1}
    got = client.get("/api/events?date=2026-06-12&run_mode=paper").json()
    assert got["schema_version"] == "0.1" and got["count"] == len(ev)
    assert client.get("/api/account-snapshots?date=2026-06-12").json()["count"] == 2
    assert client.post("/api/events", json={"events": "x"}).status_code == 422
