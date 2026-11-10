"""GET /api/scheduler：排程設定與狀態（唯讀）；最近一次執行走 scheduler.latest_runs（排程頁共用）。"""

import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server.scheduler as sched
from db.schema import create_all_tables
from server.app import create_app
from server.scheduler import JOB_DEFS

_INS = ("INSERT INTO job_runs (id, job_id, job_name, trigger_type, run_date, started_at, finished_at, duration_ms, status, "
        "summary, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")


def _client(tmp_path, runs: list[tuple] = ()) -> TestClient:
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.executemany(_INS, runs)
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False))


@pytest.fixture(autouse=True)
def _no_memory_runs():
    saved = dict(sched._last_runs)
    sched._last_runs.clear()
    yield
    sched._last_runs.clear()
    sched._last_runs.update(saved)


def test_lists_every_job_with_its_schedule_even_when_the_scheduler_is_off(tmp_path):
    body = _client(tmp_path).get("/api/scheduler").json()
    assert body["scheduler_enabled"] is False
    assert [j["id"] for j in body["jobs"]] == list(JOB_DEFS)
    bo = next(j for j in body["jobs"] if j["id"] == "before_open")
    assert bo["editable"] is True and bo["time_hhmm"] and bo["schedule_label"]
    assert bo["next_run"] is None and bo["last_run"] is None and bo["last_run_tracked"] is True


def test_last_run_is_the_latest_job_runs_row_per_job_whatever_the_insert_order(tmp_path):
    body = _client(tmp_path, [
        (1, "before_open", "盤前", "manual", "2026-06-18", "2026-06-18 08:50:00", "2026-06-18 08:50:02", 2000, "failed", None, "boom"),
        (2, "before_open", "盤前", "scheduled", "2026-06-17", "2026-06-17 08:50:00", "2026-06-17 08:50:09", 9000, "completed", "ok", None),
        (3, "verify_close", "收盤驗證", "scheduled", "2026-06-17", "2026-06-17 13:40:00", None, None, "completed", "first", None),
        (4, "verify_close", "收盤驗證", "manual", "2026-06-17", "2026-06-17 13:40:00", None, None, "partial", "same second, later id", None),
    ]).get("/api/scheduler").json()
    jobs = {j["id"]: j for j in body["jobs"]}
    assert jobs["before_open"]["last_run"] == {
        "started_at": "2026-06-18 08:50:00", "finished_at": "2026-06-18 08:50:02", "duration_ms": 2000, "status": "failed",
        "trigger_type": "manual", "run_date": "2026-06-18", "summary": None, "error": "boom", "tracked": True}
    assert jobs["verify_close"]["last_run"]["summary"] == "same second, later id"      # 同一秒：後寫的那筆
    assert jobs["after_close"]["last_run"] is None


def test_untracked_jobs_come_from_this_process_memory(tmp_path):
    """skip_logging 的盤中刷新不寫 job_runs：用本次啟動後的記憶體，標 tracked=False（不能一直顯示沒跑過）。"""
    sched._last_runs["live_refresh"] = {"time": "2026-06-18 10:00:12", "status": "error: feed down"}
    jobs = {j["id"]: j for j in _client(tmp_path).get("/api/scheduler").json()["jobs"]}
    lr = jobs["live_refresh"]
    assert lr["last_run_tracked"] is False
    assert (lr["last_run"]["started_at"], lr["last_run"]["status"], lr["last_run"]["error"], lr["last_run"]["tracked"]) == (
        "2026-06-18 10:00:12", "error", "feed down", False)


def test_with_a_running_scheduler_next_run_is_shown(tmp_path):
    client = _client(tmp_path)
    nxt = datetime(2026, 6, 19, 8, 50)
    client.app.state.scheduler = SimpleNamespace(get_job=lambda jid: SimpleNamespace(next_run_time=nxt) if jid == "before_open" else None)
    body = client.get("/api/scheduler").json()
    assert body["scheduler_enabled"] is True
    jobs = {j["id"]: j for j in body["jobs"]}
    assert jobs["before_open"]["next_run"] == "2026-06-19 08:50:00"
    assert jobs["verify_close"]["next_run"] is None


def test_scheduler_page_shows_the_same_last_run_as_the_api(tmp_path):
    client = _client(tmp_path, [
        (1, "before_open", "盤前", "scheduled", "2026-06-18", "2026-06-18 08:50:00", None, None, "completed", None, None)])
    html = client.get("/scheduler").text
    assert "2026-06-18 08:50:00" in html
    assert "（還沒有執行紀錄）" in html and "（本次啟動後尚未執行）" in html     # 有紀錄的 job 沒跑過 vs 盤中刷新
