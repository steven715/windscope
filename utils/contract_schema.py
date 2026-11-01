"""契約 schema 驗證：以 stdlib 實作的 JSON Schema 子集，驗 docs/contracts 進出的資料。

支援 type／required／enum／pattern／min／max／exclusiveMinimum／minLength／maxLength／
properties／additionalProperties／items／$ref（同檔 #/$defs）。不引第三方套件（ADR-0001）。
"""

import json
import re
from functools import lru_cache
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "contracts"

_TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    """讀 docs/contracts/{name}.schema.json（快取）。"""
    return json.loads((CONTRACTS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def schema_version(name: str) -> str:
    """契約 schema 的版本（$id 尾段，例如 0.2）。"""
    return load_schema(name)["$id"].rsplit("/", 1)[-1]


def _resolve(schema: dict, root: dict) -> dict:
    ref = schema.get("$ref")
    if not ref:
        return schema
    node = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(value, schema: dict, root: dict | None = None, path: str = "$") -> list[str]:
    """回傳違規訊息列表（空＝合法）。root 預設為 schema 本身（供 $ref）。"""
    root = root or schema
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


def validate_record(name: str, record: dict) -> list[str]:
    """用 docs/contracts/{name}.schema.json 驗一筆資料，回傳違規列表。"""
    return validate(record, load_schema(name))


class ContractError(ValueError):
    """資料不符契約 schema。"""

    def __init__(self, name: str, errors: list[str]) -> None:
        super().__init__(f"{name}: " + "; ".join(errors))
        self.contract = name
        self.errors = errors


def require_valid(name: str, record: dict) -> None:
    """不符契約就 raise ContractError。"""
    errs = validate_record(name, record)
    if errs:
        raise ContractError(name, errs)
