"""前端可送的兩個寫入（windwatch ADR-0002）：鎖定 target、立即補跑排程 job。
只收 application/json（跨站表單排不進來）；鎖定過期限 409、內容被重算 409；補跑只准清單內、排定時間後、沒在跑。"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server.scheduler as sched
import utils.tw_time as tw
from config import settings
from db.schema import create_all_tables
from db.targets import target_put
from server.app import create_app

FIX = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "fixtures"
NOT_JSON = [{"content": "x", "headers": {"Content-Type": "text/plain"}},
            {"content": "{}", "headers": {"Content-Type": "text/plain; charset=application/json"}},   # CORS 免預檢、可跨站送
            {"content": "{}", "headers": {"Content-Type": "application/jsonp"}},
            {"data": {"a": "1"}},                                                     # x-www-form-urlencoded
            {"files": {"f": ("a.txt", b"x")}},                                        # multipart
            {}]                                                                       # 沒有 Content-Type


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    tgt = None
    for line in (FIX / "targets.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            t = json.loads(line)
            target_put(conn, {**t, "locked_at": None})
            tgt = tgt or t
    conn.commit()
    conn.close()
    monkeypatch.setattr(tw, "now_tw_iso", lambda: f"{tgt['valid_from'][:10]}T08:40:00+08:00")
    monkeypatch.setattr(sched, "_running", set())
    return TestClient(create_app(db_path=db_path, enable_scheduler=False)), db_path, tgt


def _locked_at(db_path: str, tid: str):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT locked_at FROM targets WHERE target_id = ?", (tid,)).fetchone()[0]
    finally:
        conn.close()


# ── 鎖定 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("kw", NOT_JSON)
def test_lock_refuses_non_json_and_changes_nothing(setup, kw):
    client, db_path, t = setup
    assert client.post(f"/api/targets/{t['target_id']}/lock", **kw).status_code == 415
    assert _locked_at(db_path, t["target_id"]) is None


def test_lock_before_the_deadline_with_the_content_seen(setup):
    client, db_path, t = setup
    r = client.post(f"/api/targets/{t['target_id']}/lock", content=json.dumps({"produced_at": t["produced_at"]}),
                    headers={"Content-Type": "APPLICATION/JSON; charset=utf-8"})          # media type 不分大小寫
    assert r.status_code == 200 and _locked_at(db_path, t["target_id"]) == r.json()["locked_at"]


def test_lock_refuses_a_target_recomputed_since_it_was_shown(setup):
    client, db_path, t = setup
    r = client.post(f"/api/targets/{t['target_id']}/lock", json={"produced_at": "2026-06-12T08:30:00+08:00"})
    assert r.status_code == 409 and "重算" in r.json()["detail"]
    assert _locked_at(db_path, t["target_id"]) is None


def test_lock_after_the_deadline_is_refused_but_an_existing_lock_is_returned(setup, monkeypatch):
    client, db_path, t = setup
    day = t["valid_from"][:10]
    monkeypatch.setattr(tw, "now_tw_iso", lambda: f"{day}T{settings.TARGET_LOCK_DEADLINE}:01+08:00")
    r = client.post(f"/api/targets/{t['target_id']}/lock", json={})
    assert r.status_code == 409 and "期限" in r.json()["detail"]
    assert _locked_at(db_path, t["target_id"]) is None
    monkeypatch.setattr(tw, "now_tw_iso", lambda: f"{day}T{settings.TARGET_LOCK_DEADLINE}:00+08:00")    # 剛好期限那一秒：還可以
    first = client.post(f"/api/targets/{t['target_id']}/lock", json={}).json()["locked_at"]
    monkeypatch.setattr(tw, "now_tw_iso", lambda: f"{day}T09:30:00+08:00")
    r = client.post(f"/api/targets/{t['target_id']}/lock", json={})
    assert r.status_code == 200 and r.json()["locked_at"] == first                     # 已鎖定：照舊回原本的


def test_lock_rejects_a_non_json_body(setup):
    client, _, t = setup
    r = client.post(f"/api/targets/{t['target_id']}/lock", content="not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


# ── 補跑 ──────────────────────────────────────────────────────────

def _scheduler(client):
    added = []
    client.app.state.scheduler = SimpleNamespace(add_job=lambda *a, **k: added.append((k["id"], k["args"])),
                                                 get_job=lambda jid: None)
    return added


@pytest.mark.parametrize("kw", NOT_JSON)
def test_run_refuses_non_json_and_triggers_nothing(setup, kw):
    client, _, _ = setup
    added = _scheduler(client)
    assert client.post("/api/scheduler/verify_close/run", **kw).status_code == 415
    assert added == []


def test_run_only_listed_jobs(setup):
    client, _, _ = setup
    added = _scheduler(client)
    assert client.post("/api/scheduler/nope/run", json={}).status_code == 404
    for job in ("before_open", "afternoon_fx", "after_night", "live_refresh"):
        r = client.post(f"/api/scheduler/{job}/run", json={})
        assert r.status_code == 403, job
    assert added == []
    jobs = {j["id"]: j for j in client.get("/api/scheduler").json()["jobs"]}
    assert {k for k, j in jobs.items() if j["api_runnable"]} == set(settings.API_RUNNABLE_JOBS)


def test_run_is_a_catch_up_only_after_todays_scheduled_time(setup, monkeypatch):
    client, db_path, _ = setup
    added = _scheduler(client)
    at = sched.get_schedule_times(db_path)["verify_close"]
    hh, mm = map(int, at.split(":"))
    monkeypatch.setattr(tw, "now_tw", lambda: datetime(2026, 6, 12, hh, mm) - timedelta(minutes=1))
    r = client.post("/api/scheduler/verify_close/run", json={})
    assert r.status_code == 409 and at in r.json()["detail"]
    monkeypatch.setattr(tw, "now_tw", lambda: datetime(2026, 6, 12, hh, mm))
    r = client.post("/api/scheduler/verify_close/run", json={})
    assert r.status_code == 202 and r.json() == {"job_id": "verify_close", "triggered": True}
    assert added == [("verify_close__manual", [db_path])]
    monkeypatch.setattr(tw, "now_tw", lambda: datetime(2026, 6, 12, 23, 0))
    assert client.post("/api/scheduler/refresh_holidays/run", json={}).status_code == 202   # 沒有每日時間的：隨時


def test_run_refuses_when_off_or_already_running(setup, monkeypatch):
    client, _, _ = setup
    monkeypatch.setattr(tw, "now_tw", lambda: datetime(2026, 6, 12, 23, 0))
    r = client.post("/api/scheduler/after_close/run", json={})
    assert r.status_code == 409 and "scheduler" in r.json()["detail"]
    added = _scheduler(client)
    sched._running.add("after_close")
    r = client.post("/api/scheduler/after_close/run", json={})
    assert r.status_code == 409 and "正在跑" in r.json()["detail"]
    assert added == []
    assert {j["id"]: j["running"] for j in client.get("/api/scheduler").json()["jobs"]}["after_close"] is True


def test_runner_marks_the_job_running_for_both_scheduled_and_manual_runs(monkeypatch):
    seen = []
    monkeypatch.setattr(sched, "_running", set())
    monkeypatch.setattr(sched, "_make_runner_inner", lambda jid, tt="scheduled": lambda db: seen.append(sched.is_running(jid)))
    sched._make_runner("verify_close")(None)
    sched._make_runner("verify_close", trigger_type="manual")(None)
    assert seen == [True, True] and not sched.is_running("verify_close")
