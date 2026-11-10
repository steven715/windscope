"""GET /api/scheduler：排程設定與狀態（唯讀），最近一次執行取自 job_runs。"""

import sqlite3

from fastapi.testclient import TestClient

from db.schema import create_all_tables
from server.app import create_app
from server.scheduler import JOB_DEFS


def _client(tmp_path, runs: list[tuple] = ()) -> TestClient:
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.executemany("INSERT INTO job_runs (job_id, job_name, trigger_type, run_date, started_at, finished_at, "
                     "duration_ms, status, summary, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", runs)
    conn.commit()
    conn.close()
    return TestClient(create_app(db_path=db_path, enable_scheduler=False))


def test_lists_every_job_with_its_schedule_even_when_the_scheduler_is_off(tmp_path):
    body = _client(tmp_path).get("/api/scheduler").json()
    assert body["scheduler_enabled"] is False
    assert [j["id"] for j in body["jobs"]] == list(JOB_DEFS)
    bo = next(j for j in body["jobs"] if j["id"] == "before_open")
    assert bo["editable"] is True and bo["time_hhmm"] and bo["schedule_label"]
    assert bo["next_run"] is None and bo["last_run"] is None


def test_last_run_is_the_latest_job_runs_row_per_job(tmp_path):
    body = _client(tmp_path, [
        ("before_open", "盤前", "scheduled", "2026-06-17", "2026-06-17 08:50:00", "2026-06-17 08:50:09", 9000, "completed", "ok", None),
        ("before_open", "盤前", "manual", "2026-06-18", "2026-06-18 08:50:00", "2026-06-18 08:50:02", 2000, "failed", None, "boom"),
        ("verify_close", "收盤驗證", "scheduled", "2026-06-17", "2026-06-17 13:40:00", None, None, "completed", "hit", None),
    ]).get("/api/scheduler").json()
    jobs = {j["id"]: j for j in body["jobs"]}
    assert jobs["before_open"]["last_run"] == {
        "started_at": "2026-06-18 08:50:00", "finished_at": "2026-06-18 08:50:02", "duration_ms": 2000, "status": "failed",
        "trigger_type": "manual", "run_date": "2026-06-18", "summary": None, "error": "boom"}
    assert jobs["verify_close"]["last_run"]["summary"] == "hit"
    assert jobs["after_close"]["last_run"] is None
