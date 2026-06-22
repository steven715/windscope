"""run_step instrumentation 測試（保留既有回傳 + 發 step_run 事件）。"""

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


def test_run_step_ok_emits_step_run():
    """正常 step：回 (True, None) 且發 step_run outcome=ok 含 duration_ms、無 error。"""
    from jobs.helpers import run_step

    lg, h = _attach()
    try:
        ok, err = run_step("demo", lambda: True)
    finally:
        lg.removeHandler(h)

    assert ok is True and err is None
    ev = _last_step_run(h.events)
    assert ev["step"] == "demo"
    assert ev["outcome"] == "ok"
    assert isinstance(ev["duration_ms"], int)
    assert "error" not in ev


def test_run_step_no_data_emits_failed():
    """回 None → outcome=failed、error 含 no data，回傳仍是 (False, err)。"""
    from jobs.helpers import run_step

    lg, h = _attach()
    try:
        ok, err = run_step("nodata", lambda: None)
    finally:
        lg.removeHandler(h)

    assert ok is False
    assert "no data" in err
    ev = _last_step_run(h.events)
    assert ev["outcome"] == "failed"
    assert "no data" in ev["error"]


def test_run_step_exception_emits_failed_and_not_raise():
    """step 拋例外 → 不 raise、回 (False, err)，發 outcome=failed 含錯誤訊息。"""
    from jobs.helpers import run_step

    def boom():
        raise ValueError("kaboom")

    lg, h = _attach()
    try:
        ok, err = run_step("boom", boom)
    finally:
        lg.removeHandler(h)

    assert ok is False
    assert "kaboom" in err
    ev = _last_step_run(h.events)
    assert ev["outcome"] == "failed"
    assert "kaboom" in ev["error"]
