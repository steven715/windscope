"""契約 fixture 必須符合 schema（docs/contracts）。

純 stdlib 的 JSON Schema 子集驗證器（type/required/enum/pattern/min/max/properties/$ref 本檔內
定義/additionalProperties），不引第三方。tradewind 端應有對稱的測試讀同一份 fixture。
"""

import json
import re
from pathlib import Path

import pytest

CONTRACTS = Path(__file__).resolve().parents[2] / "docs" / "contracts"
TABLES = ["bars", "instruments", "corporate_actions", "targets"]

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def _resolve(schema: dict, root: dict) -> dict:
    ref = schema.get("$ref")
    if not ref:
        return schema
    assert ref.startswith("#/"), ref
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(value, schema: dict, root: dict, path: str = "$") -> list[str]:
    """回傳違規訊息列表（空＝合法）。"""
    schema = _resolve(schema, root)
    errs: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_TYPES[t](value) for t in allowed):
            return [f"{path}: type {type(value).__name__} not in {allowed}"]
    if value is None:
        return errs
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: {value!r} !~ {schema['pattern']}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: {value} > {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errs.append(f"{path}: {value} <= {schema['exclusiveMinimum']}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                errs.append(f"{path}: missing required {k!r}")
        if schema.get("additionalProperties") is False:
            for k in value:
                if k not in props:
                    errs.append(f"{path}: unexpected key {k!r}")
        for k, v in value.items():
            if k in props:
                errs.extend(validate(v, props[k], root, f"{path}.{k}"))
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs.extend(validate(v, schema["items"], root, f"{path}[{i}]"))
    return errs


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
    """同 (strategy_id, instrument_id, valid_from) 只能有一個 target_id。"""
    _, rows = _load("targets")
    seen: dict[tuple, str] = {}
    for r in rows:
        key = (r["strategy_id"], r["instrument_id"], r["valid_from"])
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
