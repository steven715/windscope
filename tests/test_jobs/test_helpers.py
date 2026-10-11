"""jobs.helpers 測試：run_step_value 發 step_run 事件、JobRun 紀錄步驟與 job_completed。"""

import json
import logging


class _EventCapture(logging.Handler):
    """收集 windscope.events 的事件（解析成 dict）。"""

    def __init__(self):
        super().__init__()
        self.events = []

    def emit(self, record):
        try:
            self.events.append(json.loads(record.getMessage()))
        except Exception:
            pass


def _attach():
    lg = logging.getLogger("windscope.events")
    h = _EventCapture()
    h.setLevel(logging.DEBUG)
    lg.addHandler(h)
    return lg, h


def _last_step_run(events):
    return [e for e in events if e.get("event") == "step_run"][-1]


def test_run_step_value_ok_emits_step_run():
    """正常 step：回 (結果, None) 且發 step_run outcome=ok 含 duration_ms、無 error。"""
    from jobs.helpers import run_step_value

    lg, h = _attach()
    try:
        value, err = run_step_value("demo", lambda: True)
    finally:
        lg.removeHandler(h)

    assert value is True and err is None
    ev = _last_step_run(h.events)
    assert ev["step"] == "demo"
    assert ev["outcome"] == "ok"
    assert isinstance(ev["duration_ms"], int)
    assert "error" not in ev


def test_run_step_value_no_data_emits_failed():
    """回 None → outcome=failed、error 含 no data，回傳 (None, err)。"""
    from jobs.helpers import run_step_value

    lg, h = _attach()
    try:
        value, err = run_step_value("nodata", lambda: None)
    finally:
        lg.removeHandler(h)

    assert value is None
    assert "no data" in err
    ev = _last_step_run(h.events)
    assert ev["outcome"] == "failed"
    assert "no data" in ev["error"]


def test_run_step_value_exception_emits_failed_and_not_raise():
    """step 拋例外 → 不 raise、回 (None, err)，發 outcome=failed 含錯誤訊息。"""
    from jobs.helpers import run_step_value

    def boom():
        raise ValueError("kaboom")

    lg, h = _attach()
    try:
        value, err = run_step_value("boom", boom)
    finally:
        lg.removeHandler(h)

    assert value is None
    assert "kaboom" in err
    ev = _last_step_run(h.events)
    assert ev["outcome"] == "failed"
    assert "kaboom" in ev["error"]


def test_job_run_records_steps_and_emits_job_completed():
    """step 回傳值、collect 存資料；失敗與 no-data 記為 False；finish 判定 partial 並發事件。"""
    from jobs.helpers import JobRun

    saved = []
    lg, h = _attach()
    try:
        job = JobRun("demo_job", "2026-06-15")
        assert job.step("calc", lambda: {"v": 1}) == {"v": 1}
        assert job.step("nodata", lambda: None) is None
        assert job.collect("fetch", lambda: [1, 2], saved.append) is True
        assert job.collect("fetch_none", lambda: None, saved.append) is False
        result = job.finish(extra_flag=True)
    finally:
        lg.removeHandler(h)

    assert saved == [[1, 2]]
    assert result == {
        "date": "2026-06-15", "status": "partial",
        "results": {"calc": True, "nodata": False, "fetch": True, "fetch_none": False},
        "errors": ["nodata: no data", "fetch_none: no data"],
    }
    ev = [e for e in h.events if e.get("event") == "job_completed"][-1]
    assert (ev["job"], ev["status"], ev["steps_ok"], ev["steps_failed"]) == (
        "demo_job", "partial", 2, 2)
    assert ev["extra_flag"] is True


def test_job_run_skipped_shape():
    from jobs.helpers import JobRun

    assert JobRun.skipped("demo_job", "2026-06-14", "週末", summary=None) == {
        "date": "2026-06-14", "status": "skipped", "results": {}, "errors": [],
        "summary": None}
