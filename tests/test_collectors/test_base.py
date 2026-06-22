"""BaseCollector.run instrumentation 測試：三分支 outcome 對應 collector_run 事件。"""

import json
import logging

from collectors.base import BaseCollector


class _EventCapture(logging.Handler):
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


def _last_collector_run(events):
    return [e for e in events if e.get("event") == "collector_run"][-1]


class _OkCollector(BaseCollector):
    def collect(self, date):
        return {"x": 1}

    def save(self, date, data):
        pass


class _NoDataCollector(BaseCollector):
    def collect(self, date):
        return None

    def save(self, date, data):
        pass


class _FailCollector(BaseCollector):
    def collect(self, date):
        raise RuntimeError("collect blew up")

    def save(self, date, data):
        pass


def test_collector_run_ok():
    """collect 有資料且 save 成功 → outcome=ok，含 collector/date/duration_ms。"""
    lg, h = _attach()
    try:
        ok = _OkCollector(db_path=":memory:").run("2026-06-15")
    finally:
        lg.removeHandler(h)

    assert ok is True
    ev = _last_collector_run(h.events)
    assert ev["collector"] == "_OkCollector"
    assert ev["date"] == "2026-06-15"
    assert ev["outcome"] == "ok"
    assert isinstance(ev["duration_ms"], int)


def test_collector_run_no_data():
    """collect 回 None → outcome=no_data，run() 仍回 False。"""
    lg, h = _attach()
    try:
        ok = _NoDataCollector(db_path=":memory:").run("2026-06-15")
    finally:
        lg.removeHandler(h)

    assert ok is False
    assert _last_collector_run(h.events)["outcome"] == "no_data"


def test_collector_run_failed():
    """collect 拋例外 → outcome=failed，run() 不 raise、回 False。"""
    lg, h = _attach()
    try:
        ok = _FailCollector(db_path=":memory:").run("2026-06-15")
    finally:
        lg.removeHandler(h)

    assert ok is False
    assert _last_collector_run(h.events)["outcome"] == "failed"
