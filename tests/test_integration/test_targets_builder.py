"""盤前判斷 → 契約 target：方向／權重、失效條件、冪等 id、鎖定保留、無判斷回 None。"""

import json
import sqlite3
from pathlib import Path

import pytest

from config import settings
from db.schema import create_all_tables
from db.targets import target_get, target_lock
from integration.targets_builder import STRATEGY_ID, build_premarket_target, target_id_for
from utils.contract_schema import validate_record

# 2026-06-12 週五；前一交易日 06-11
D, PREV = "2026-06-12", "2026-06-11"


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    return conn


def _signal(db, direction="bullish", confidence=3, rule_version="v3", reasons=("匯率與期貨同向", "亞幣同步 +1")):
    db.execute("INSERT INTO signals (date, direction, confidence, rule_version, reasons) VALUES (?, ?, ?, ?, ?)",
               (D, direction, confidence, rule_version, json.dumps(list(reasons), ensure_ascii=False)))


def _prev_index(db, low=22850.0, high=23010.0):
    db.execute("INSERT INTO raw_index (date, open, high, low, close) VALUES (?, 22900, ?, ?, 22980)", (PREV, high, low))


def test_bullish_target_matches_contract(db):
    _signal(db); _prev_index(db)
    rec = build_premarket_target(D, db, produced_at="2026-06-12T08:50:07+08:00")
    assert validate_record("targets", rec) == []
    assert rec["instrument_id"] == settings.PREMARKET_TARGET_INSTRUMENT
    assert rec["target_weight"] == 0.6 and rec["conviction"] == 0.6 and rec["horizon"] == "intraday"
    assert rec["valid_from"] == f"{D}T08:50:00+08:00" and rec["valid_until"] == f"{D}T13:25:00+08:00"
    assert rec["locked_at"] is None
    cond = rec["invalidation"]["conditions"][0]
    assert cond == {"instrument_id": "TWSE:TAIEX", "field": "low", "op": "<", "value": 22850.0,
                    "until": f"{D}T09:30:00+08:00"}
    assert rec["trade_plan"]["playbook_id"] == settings.PREMARKET_PLAYBOOK["playbook_id"]
    assert rec["trade_plan"]["max_entries_per_day"] == 1 and rec["trade_plan"]["risk"]["stop_pts"] == 30
    assert rec["reason"] == "匯率與期貨同向；亞幣同步 +1"
    assert rec["inputs_ref"] == {"source_table": "signals", "key": {"date": D}, "inputs_hash": None}
    assert target_get(db, rec["target_id"]) == rec                  # 已寫入 targets 表


def test_bearish_uses_prev_high_and_negative_weight(db):
    _signal(db, "bearish", 5); _prev_index(db)
    rec = build_premarket_target(D, db)
    assert rec["target_weight"] == -1.0
    cond = rec["invalidation"]["conditions"][0]
    assert (cond["field"], cond["op"], cond["value"]) == ("high", ">", 23010.0)


def test_neutral_has_zero_weight_and_text_only_invalidation(db):
    _signal(db, "neutral", 2); _prev_index(db)
    rec = build_premarket_target(D, db)
    assert rec["target_weight"] == 0.0 and rec["conviction"] == 0.4
    assert rec["invalidation"] == {"text": "中性無部位，無失效條件"}


def test_missing_prev_index_keeps_text_only(db):
    _signal(db)
    rec = build_premarket_target(D, db)
    assert "conditions" not in rec["invalidation"] and "僅人工判定" in rec["invalidation"]["text"]
    assert validate_record("targets", rec) == []


def test_no_signal_returns_none(db):
    assert build_premarket_target(D, db) is None


def test_target_id_deterministic_and_rebuild_keeps_lock(db):
    _signal(db); _prev_index(db)
    a = build_premarket_target(D, db)
    assert a["target_id"] == target_id_for(STRATEGY_ID, a["instrument_id"], a["valid_from"], "v3")
    target_lock(db, a["target_id"], "2026-06-12T08:48:00+08:00")
    b = build_premarket_target(D, db)                               # 重算（例如 recompute）
    assert b["target_id"] == a["target_id"]
    assert target_get(db, a["target_id"])["locked_at"] == "2026-06-12T08:48:00+08:00"


def test_rebuild_after_lock_keeps_locked_content_unless_forced(db, caplog):
    """鎖定後訊號變了（例如 recompute）：不覆蓋、記 warning、回傳既有；force 才覆蓋。"""
    _signal(db); _prev_index(db)
    a = build_premarket_target(D, db)
    target_lock(db, a["target_id"], "2026-06-12T08:48:00+08:00")
    db.execute("UPDATE signals SET direction = 'bearish' WHERE date = ?", (D,))
    with caplog.at_level("WARNING"):
        b = build_premarket_target(D, db)
    assert b["target_weight"] == a["target_weight"] and "鎖定，不可修改" in caplog.text
    c = build_premarket_target(D, db, force=True)
    assert c["target_weight"] < 0 and target_get(db, a["target_id"])["locked_at"] == "2026-06-12T08:48:00+08:00"


def test_fixture_target_ids_match_hash():
    """契約 fixture 的 target_id 必須等於冪等鍵的雜湊（tradewind 的對稱測試會重算）。"""
    fix = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "fixtures" / "targets.jsonl"
    for line in fix.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            assert r["target_id"] == target_id_for(r["strategy_id"], r["instrument_id"], r["valid_from"], r["rule_version"])


def test_rule_version_changes_target_id(db):
    _signal(db, rule_version="v3"); _prev_index(db)
    a = build_premarket_target(D, db)
    db.execute("UPDATE signals SET rule_version = 'v4' WHERE date = ?", (D,))
    b = build_premarket_target(D, db)
    assert a["target_id"] != b["target_id"]
