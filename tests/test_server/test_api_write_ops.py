"""前端可送的兩個寫入（windwatch ADR-0002）：鎖定 target、立即執行排程 job。只收 application/json（跨站表單排不進來）。"""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from db.schema import create_all_tables
from db.targets import target_put
from server.app import create_app

FIX = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "fixtures"
NOT_JSON = [{"content": "x", "headers": {"Content-Type": "text/plain"}},
            {"data": {"a": "1"}},                                                     # x-www-form-urlencoded
            {"files": {"f": ("a.txt", b"x")}},                                        # multipart
            {}]                                                                       # 沒有 Content-Type


@pytest.fixture
def setup(tmp_path):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    tid = None
    for line in (FIX / "targets.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            t = json.loads(line)
            target_put(conn, {**t, "locked_at": None})
            tid = tid or t["target_id"]
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False)), db_path, tid


def _locked_at(db_path: str, tid: str):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT locked_at FROM targets WHERE target_id = ?", (tid,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("kw", NOT_JSON)
def test_lock_refuses_non_json_and_changes_nothing(setup, kw):
    client, db_path, tid = setup
    assert client.post(f"/api/targets/{tid}/lock", **kw).status_code == 415
    assert _locked_at(db_path, tid) is None


def test_lock_with_json_locks(setup):
    client, db_path, tid = setup
    r = client.post(f"/api/targets/{tid}/lock", json={}, headers={"Content-Type": "application/json; charset=utf-8"})
    assert r.status_code == 200 and _locked_at(db_path, tid) == r.json()["locked_at"]


@pytest.mark.parametrize("kw", NOT_JSON)
def test_run_refuses_non_json_and_triggers_nothing(setup, kw):
    client, _, _ = setup
    added = []
    client.app.state.scheduler = SimpleNamespace(add_job=lambda *a, **k: added.append(k["id"]))
    assert client.post("/api/scheduler/before_open/run", **kw).status_code == 415
    assert added == []


def test_run_unknown_job_404_and_scheduler_off_409(setup):
    client, _, _ = setup
    assert client.post("/api/scheduler/nope/run", json={}).status_code == 404
    r = client.post("/api/scheduler/before_open/run", json={})
    assert r.status_code == 409 and "scheduler" in r.json()["detail"]


def test_run_triggers_a_manual_run_in_the_background(setup):
    client, _, _ = setup
    added = []
    client.app.state.scheduler = SimpleNamespace(add_job=lambda *a, **k: added.append(k["id"]))
    r = client.post("/api/scheduler/before_open/run", json={})
    assert r.status_code == 202 and r.json() == {"job_id": "before_open", "triggered": True}
    assert added == ["before_open__manual"]
