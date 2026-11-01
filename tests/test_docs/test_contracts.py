"""契約 fixture 必須符合 schema（docs/contracts）。

驗證器在 utils/contract_schema.py（stdlib JSON Schema 子集）；本檔鎖 fixture 與契約語意。
tradewind 端應有對稱的測試讀同一份 fixture。
"""

import json
from pathlib import Path

import pytest

CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "contracts"
TABLES = ["bars", "instruments", "corporate_actions", "targets", "events", "account_snapshots"]

from utils.contract_schema import validate  # noqa: E402  驗證器正式版在 utils


def _load(table: str):
    schema = json.loads((CONTRACTS / f"{table}.schema.json").read_text(encoding="utf-8"))
    lines = (CONTRACTS / "fixtures" / f"{table}.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(ln) for ln in lines if ln.strip()]
    return schema, rows


@pytest.mark.parametrize("table", TABLES)
def test_fixtures_match_schema(table):
    schema, rows = _load(table)
    assert rows, f"{table} fixture is empty"
    for i, row in enumerate(rows):
        errs = validate(row, schema, schema, f"{table}[{i}]")
        assert not errs, "\n".join(errs)


def test_validator_rejects_bad_rows():
    """驗證器本身要會抓錯，不然上面的測試是假綠。"""
    schema, rows = _load("targets")
    bad = dict(rows[0])
    bad["target_weight"] = 1.5                      # 超出 [-1, 1]
    bad["horizon"] = "forever"                      # 不在 enum
    bad.pop("valid_from")                           # 缺必填
    bad["extra"] = 1                                # 多餘欄位
    errs = validate(bad, schema, schema)
    joined = "\n".join(errs)
    assert "target_weight" in joined and "horizon" in joined
    assert "valid_from" in joined and "extra" in joined


def test_targets_are_idempotent_by_key():
    """同 (strategy_id, instrument_id, valid_from, rule_version) 只能有一個 target_id。"""
    _, rows = _load("targets")
    seen: dict[tuple, str] = {}
    for r in rows:
        key = (r["strategy_id"], r["instrument_id"], r["valid_from"], r["rule_version"])
        assert seen.setdefault(key, r["target_id"]) == r["target_id"], key


def test_targets_only_reference_tradable_instruments():
    """targets 不得指向 tradable=false 的商品（指數、匯率）。"""
    _, instruments = _load("instruments")
    tradable = {i["instrument_id"] for i in instruments if i["tradable"]}
    _, targets = _load("targets")
    for t in targets:
        assert t["instrument_id"] in tradable, t["instrument_id"]


def test_night_bars_attribute_to_next_session_date():
    """夜盤 bar 的 session_date 必須晚於 ts 的日曆日（夜盤歸次一交易日）。"""
    _, bars = _load("bars")
    for b in bars:
        if b["session"] == "night":
            assert b["session_date"] > b["ts"][:10], b


def test_events_reference_known_targets_and_tradable_instruments():
    """events 的 target_id 必須存在於 targets fixture；instrument 必須可交易；event_id 唯一。"""
    _, targets = _load("targets")
    _, instruments = _load("instruments")
    _, events = _load("events")
    target_ids = {t["target_id"] for t in targets}
    tradable = {i["instrument_id"] for i in instruments if i["tradable"]}
    seen = set()
    for e in events:
        assert e["event_id"] not in seen, e["event_id"]
        seen.add(e["event_id"])
        if e["target_id"] is not None:
            assert e["target_id"] in target_ids, e["target_id"]
        if e["instrument_id"] is not None:
            assert e["instrument_id"] in tradable, e["instrument_id"]
        if e["kind"] == "override":
            assert e["reason"], "override 必須有理由"
            assert e["action"] in ("early_exit", "reduce")


def test_stream_bars_never_partial():
    """串流只推收完的 K 棒：K 棒的 published_at（可知時刻）不早於 ts + 粒度；tick 的 published_at = ts。"""
    _, bars = _load("bars")
    for b in bars:
        if b["resolution"] == "tick":
            assert b["published_at"] == b["ts"], b
        elif b["resolution"] in ("1m", "5m"):
            assert b["published_at"][11:16] > b["ts"][11:16], b
        assert b["ingested_at"] >= b["published_at"], b   # 拿到不可能早於可知


def test_targets_locked_and_single_entry():
    """v0.2：每筆 target 有 locked_at（可 null）、trade_plan 帶 params 與 max_entries_per_day=1。"""
    _, rows = _load("targets")
    for r in rows:
        assert "locked_at" in r
        assert r["trade_plan"]["max_entries_per_day"] == 1
        assert isinstance(r["trade_plan"]["params"], dict) and r["trade_plan"]["params"]
