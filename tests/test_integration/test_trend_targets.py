"""個股趨勢訊號 → 契約 target：首批／加倉（冷卻、不宜追高）／減倉／大勢轉向出場／不變不發、閘門假設、
失效與停損距離、已在失效價位下不開倉、商品不可交易、冪等與鎖定、watchlist 迴圈一檔失敗不影響其他檔。"""

import json
import sqlite3

import pytest

from config import settings
from db.instruments import instrument_upsert
from db.schema import create_all_tables
from db.targets import target_get, target_lock, targets_list
from integration.trend_targets import (
    STRATEGY_ID, build_trend_target, build_trend_targets, cumulative_weight, decide_units,
)
from utils.contract_schema import validate_record

# 2026-06-12（五）→ 次一交易日 06-15（一）
D, NEXT = "2026-06-12", "2026-06-15"
INST = {"instrument_id": "TWSE:2330", "market": "TWSE", "symbol": "2330", "name": "台積電", "asset_class": "equity",
        "currency": "TWD", "tradable": True, "tick_size": 1.0, "lot_size": 1000, "multiplier": 1,
        "price_limit_pct": 10, "underlying": None, "expiry": None, "roll_rule": None,
        "sessions": {"regular": {"open": "09:00", "close": "13:30"}}, "active": True, "as_of": "2026-06-01",
        "initial_margin": None, "maintenance_margin": None, "margin_source": None}


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    instrument_upsert(conn, INST)
    conn.execute("INSERT INTO watchlist (stock_id, stock_name) VALUES ('2330', '台積電')")
    return conn


def _sig(db, date=D, stock="2330", big="UP", action="關注建倉區", position="可加倉", gate="PASS",
         small="貼近中樞", turn_up=1, center=960.0, conf=0.72, close=980.0, rv="t2"):
    db.execute(
        "INSERT OR REPLACE INTO stock_trend_signals (date, stock_id, big_trend, big_trend_confidence, value_center, "
        "bias_pct, small_state, small_turn_up, fundamental_gate, action_hint, position_hint, reasons, rule_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (date, stock, big, conf, center, 0.02, small, turn_up, gate, action, position, json.dumps(["r"]), rv))
    if close is not None:
        db.execute("INSERT OR REPLACE INTO raw_stock_daily (date, stock_id, open, high, low, close, volume) "
                   "VALUES (?, ?, ?, ?, ?, ?, 1)", (date, stock, close, close, close, close))


def test_tranche_weights_three_equal_by_default():
    assert settings.BACKTEST_MAX_UNITS == 3 and settings.BACKTEST_TRANCHE_SIZING == "equal"
    assert [cumulative_weight(u) for u in (0, 1, 2, 3, 4)] == [0.0, 0.3333, 0.6667, 1.0, 1.0]


def test_decide_units_mirrors_backtest_branches():
    assert decide_units(0, "UP", "關注建倉區", "可加倉", None)[0] == 1
    assert decide_units(0, "UP", "觀望", "可加倉", None)[0] == 0
    assert decide_units(1, "UP", "觀望", "可加倉", 3)[0] == 2                 # 冷卻到了 → 加
    assert decide_units(1, "UP", "觀望", "可加倉", 2)[0] == 1                 # 冷卻未到
    assert decide_units(1, "UP", "不宜追高", "可加倉", 5)[0] == 1             # 追高不加
    assert decide_units(3, "UP", "觀望", "可加倉", 9)[0] == 3                 # 滿批
    assert decide_units(2, "UP", "觀望", "減倉", None)[0] == 1
    assert decide_units(2, "FLAT", "震盪不操作", "持有", None)[0] == 0
    assert decide_units(0, "DOWN", "迴避", "—", None)[0] == 0


def test_first_entry_target_matches_contract(db):
    _sig(db)
    rec = build_trend_target(D, "2330", db, produced_at=f"{D}T18:45:10+08:00")
    assert validate_record("targets", rec) == []
    assert rec["strategy_id"] == STRATEGY_ID and rec["instrument_id"] == "TWSE:2330"
    assert rec["target_weight"] == 0.3333 and rec["conviction"] == 0.72 and rec["horizon"] == "swing"
    assert rec["valid_from"] == f"{NEXT}T09:00:00+08:00" and rec["valid_until"] is None and rec["locked_at"] is None
    assert rec["rule_version"] == f"t2-{settings.BACKTEST_RULE_VERSION}"
    inv = rec["invalidation"]["conditions"][0]
    assert inv == {"instrument_id": "TWSE:2330", "field": "close", "op": "<", "value": 940.8, "until": None}
    tp = rec["trade_plan"]
    assert (tp["playbook_id"], tp["playbook_version"]) == ("trend_pullback", "p1")
    assert tp["params"] == settings.TREND_PLAYBOOK["params"] and tp["max_entries_per_day"] == 1
    assert tp["risk"]["stop_pts"] == pytest.approx(980 - 940.8) and tp["risk"]["max_units"] == 3
    assert rec["inputs_ref"]["key"] == {"date": D, "stock_id": "2330", "units": 1, "last_add_date": D}
    assert "第 1 批" in rec["reason"]
    assert target_get(db, rec["target_id"]) == rec


def test_unchanged_units_emit_nothing(db):
    _sig(db, action="觀望")
    assert build_trend_target(D, "2330", db) is None and targets_list(db) == []
    _sig(db); build_trend_target(D, "2330", db)                                   # 第 1 批
    _sig(db, date="2026-06-15", action="觀望", position="持有", close=990.0)
    assert build_trend_target("2026-06-15", "2330", db) is None                   # 持有不變 → 不發
    assert len(targets_list(db)) == 1


def test_add_respects_cooldown_then_adds_second_tranche(db):
    _sig(db); first = build_trend_target(D, "2330", db)
    _sig(db, date="2026-06-16", action="觀望", position="可加倉", close=970.0)     # 06-12 → 06-16 只隔 2 個交易日
    assert build_trend_target("2026-06-16", "2330", db) is None
    _sig(db, date="2026-06-17", action="觀望", position="可加倉", close=975.0, center=962.0)   # 隔 3 個交易日
    rec = build_trend_target("2026-06-17", "2330", db)
    assert rec["target_weight"] == 0.6667 and rec["inputs_ref"]["key"]["units"] == 2
    assert rec["inputs_ref"]["key"]["last_add_date"] == "2026-06-17"
    assert rec["trade_plan"]["risk"]["stop_pts"] == pytest.approx(975 - round(962 * 0.98, 2))   # 加倉重算停損
    assert rec["target_id"] != first["target_id"] and validate_record("targets", rec) == []


def test_trim_carries_previous_plan_and_one_tranche_trim_means_flat(db):
    _sig(db); build_trend_target(D, "2330", db)                                  # 1 批
    _sig(db, date="2026-06-17", action="觀望", position="可加倉", close=975.0, center=962.0)
    second = build_trend_target("2026-06-17", "2330", db)                         # 2 批（冷卻 3 日到）
    _sig(db, date="2026-06-18", action="不宜追高", position="減倉", small="超買遠離", close=1100.0)
    rec = build_trend_target("2026-06-18", "2330", db)                            # 減一批 → 1 批
    assert rec["target_weight"] == 0.3333 and rec["inputs_ref"]["key"]["units"] == 1
    assert rec["trade_plan"] == second["trade_plan"] and rec["invalidation"] == second["invalidation"]  # 沿用、不重算
    assert rec["inputs_ref"]["key"]["last_add_date"] == "2026-06-17" and "減倉" in rec["reason"]
    _sig(db, date="2026-06-19", action="不宜追高", position="減倉", small="超買遠離", close=1150.0)
    rec = build_trend_target("2026-06-19", "2330", db)                            # 再減 → 0 批＝全出
    assert rec["target_weight"] == 0.0 and rec["inputs_ref"]["key"]["units"] == 0
    assert validate_record("targets", rec) == []


def test_trend_exit_emits_weight_zero_with_previous_plan(db):
    _sig(db); first = build_trend_target(D, "2330", db)
    _sig(db, date="2026-06-15", big="FLAT", action="震盪不操作", position="持有", close=950.0, center=None)
    rec = build_trend_target("2026-06-15", "2330", db)
    assert rec["target_weight"] == 0.0 and rec["inputs_ref"]["key"]["units"] == 0
    assert rec["trade_plan"] == first["trade_plan"] and rec["invalidation"] == first["invalidation"]
    assert rec["inputs_ref"]["key"]["last_add_date"] == D and "離開 UP" in rec["reason"]
    assert validate_record("targets", rec) == []
    _sig(db, date="2026-06-16", big="DOWN", action="迴避", position="—", close=940.0)
    assert build_trend_target("2026-06-16", "2330", db) is None                  # 已 0 批 → 不再發


def test_gate_unknown_assumed_pass_but_fail_blocks(db, monkeypatch):
    _sig(db, gate="UNKNOWN", action="觀望")                                       # 引擎在 UNKNOWN 下不給建倉提示
    rec = build_trend_target(D, "2330", db)
    assert rec is not None and rec["target_weight"] == 0.3333                    # 回測假設 PASS → 重算成關注建倉區
    db.execute("DELETE FROM targets")
    monkeypatch.setattr(settings, "BACKTEST_ASSUME_GATE_PASS", False)
    assert build_trend_target(D, "2330", db) is None
    monkeypatch.setattr(settings, "BACKTEST_ASSUME_GATE_PASS", True)
    _sig(db, gate="FAIL", action="迴避")
    assert build_trend_target(D, "2330", db) is None


def test_no_entry_when_close_already_below_invalidation(db, caplog):
    _sig(db, small="跌破恐慌", close=930.0, center=960.0)                         # 失效價位 940.8 > 收盤
    assert build_trend_target(D, "2330", db) is None and "失效價位" in caplog.text
    _sig(db, close=None)
    assert build_trend_target(D, "2330", db) is None                            # 無收盤價也不開


def test_untradable_or_unknown_instrument_is_skipped(db):
    _sig(db, stock="9999")
    assert build_trend_target(D, "9999", db) is None
    db.execute("UPDATE instruments SET tradable = 0, payload = ? WHERE instrument_id = 'TWSE:2330'",
               (json.dumps({**INST, "tradable": False}),))
    _sig(db)
    assert build_trend_target(D, "2330", db) is None


def test_idempotent_rebuild_keeps_lock_and_id(db):
    _sig(db); rec = build_trend_target(D, "2330", db)
    target_lock(db, rec["target_id"], f"{D}T20:10:00+08:00")
    again = build_trend_target(D, "2330", db)
    assert again["target_id"] == rec["target_id"] and again["locked_at"] == f"{D}T20:10:00+08:00"
    assert len(targets_list(db)) == 1


def test_build_all_watchlist_isolates_failures(db, monkeypatch):
    db.execute("INSERT INTO watchlist (stock_id, stock_name) VALUES ('0050', 'ETF')")
    instrument_upsert(db, {**INST, "instrument_id": "TWSE:0050", "symbol": "0050", "name": "0050"})
    _sig(db); _sig(db, stock="0050", center=180.0, close=185.0)
    import integration.trend_targets as tt
    real = tt.build_trend_target

    def boom(date, stock_id, conn, **kw):
        if stock_id == "0050":
            raise RuntimeError("boom")
        return real(date, stock_id, conn, **kw)
    monkeypatch.setattr(tt, "build_trend_target", boom)
    recs = build_trend_targets(D, db)
    assert [r["instrument_id"] for r in recs] == ["TWSE:2330"]
