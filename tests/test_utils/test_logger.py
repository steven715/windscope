"""log_event 結構化事件 helper 測試。"""

import json
import logging

from utils.logger import log_event


class _CaptureHandler(logging.Handler):
    """收集流經 windscope.events 的 record（不依賴 propagate / setup_logging）。"""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture():
    """掛一個 capture handler 到 windscope.events，回傳 (logger, handler)。"""
    lg = logging.getLogger("windscope.events")
    h = _CaptureHandler()
    h.setLevel(logging.DEBUG)
    lg.addHandler(h)
    return lg, h


def test_log_event_emits_parsable_json():
    """happy：輸出一行可被 json.loads 解析的事件，含 event/ts 與自訂欄位。"""
    lg, h = _capture()
    try:
        log_event("unit_test_event", foo="bar", n=3)
    finally:
        lg.removeHandler(h)

    assert h.records, "應發出至少一筆事件"
    payload = json.loads(h.records[-1].getMessage())
    assert payload["event"] == "unit_test_event"
    assert payload["foo"] == "bar"
    assert payload["n"] == 3
    assert "ts" in payload
    assert h.records[-1].levelno == logging.INFO


def test_log_event_level_respected():
    """指定 level 會反映在 log record 上。"""
    lg, h = _capture()
    try:
        log_event("warn_event", level=logging.WARNING)
    finally:
        lg.removeHandler(h)

    assert h.records[-1].levelno == logging.WARNING


def test_log_event_never_raises_on_weird_fields():
    """failure：丟非 JSON 序列化的 field（set / 物件）也不能 raise。"""
    lg, h = _capture()
    try:
        # 不 raise 即通過（default=str 容錯，仍應發出一筆可解析的事件）
        log_event("weird", a={1, 2, 3}, b=object())
    finally:
        lg.removeHandler(h)

    assert h.records
    json.loads(h.records[-1].getMessage())  # 可解析（非原生型別被 str 化）
