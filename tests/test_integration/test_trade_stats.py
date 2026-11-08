"""integration/trade_stats（依成交打分）：分批出場的加權 R、未平倉不算、出場原因（停損／失效／目標／override 分開）、
同日重跑分開、run_mode 不混、版本混用標記、Insight 對錯對到驗證、帳戶權益曲線、沒資料；/api/trade-stats 與 /stats 頁。"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from db.events import events_ingest, snapshots_ingest
from db.schema import create_all_tables
from db.targets import target_put
from integration.trade_stats import build_trade_stats, build_trades, exit_reason
from server.app import create_app

FIX = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "fixtures"
D = "2026-06-12"
TGT = "0765ce4b0c35f0c0"


def _fixture(name: str) -> list[dict]:
    return [json.loads(x) for x in (FIX / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


_SEQ = [0]


def _ev(kind, action, ts, side=None, qty=None, price=None, r=None, reason=None, target=TGT, run="", mode="paper",
        engine="g13", strategy="premarket_call", detail=None):
    _SEQ[0] += 1
    return {"schema_version": "0.1", "event_id": f"sim-fut-01-e{_SEQ[0]:04d}{run}", "run_mode": mode, "account_id": "sim-fut-01",
            "strategy_id": strategy, "target_id": target, "instrument_id": "TAIFEX:TMF", "ts": f"{D}T{ts}:00+08:00",
            "recorded_at": f"{D}T{ts}:00+08:00", "kind": kind, "action": action, "side": side, "qty": qty, "price": price,
            "r_multiple": r, "reason": reason, "detail": detail if detail is not None else ({"commission": 10, "tax": 2} if kind == "fill" else None),
            "versions": {"execution_version": "e3", "risk_version": "r8", "engine_version": engine, "cost_model": "sim-c4/cost-c2"}}


def _trade(entry_ts, exits, end, target=TGT, **kw):
    """一筆交易：進場 buy 10 → 出場（[(ts, qty, r)]）→ 終局事件 end＝(kind, action, reason)。"""
    rows = [_ev("plan", "entered", entry_ts, "buy", 10, 23000.0, 0.0, target=target, **kw),
            _ev("fill", "fill", entry_ts, "buy", 10, 23000.0, 0.0, target=target, **kw)]
    for ts, q, r in exits:
        rows.append(_ev("fill", "fill", ts, "sell", q, 23000.0 + 30 * (r or 0), r, target=target, **kw))
    if end:
        rows.append(_ev(end[0], end[1], exits[-1][0], "sell", exits[-1][1], None, exits[-1][2], end[2], target=target, **kw))
    return rows


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    target_put(conn, _fixture("targets")[0])
    yield conn
    conn.close()


def _ingest(conn, rows):
    res = events_ingest(conn, rows)
    assert res["invalid"] == 0, res["errors"][:2]


def test_exit_reason_classification():
    assert exit_reason("override", "early_exit", "x") == "override"
    assert exit_reason("plan", "invalidated", "期貨跌破") == "invalidated"
    assert exit_reason("plan", "force_flat", "force_flat") == "force_flat"
    assert [exit_reason("plan", "exited", r) for r in ("stop", "runner", "scaled_out_all", "superseded by abc", "flatten")] == [
        "stop", "target", "target", "superseded", "other"]


def test_weighted_r_reasons_overrides_insight_and_versions(db):
    rows = (_trade("09:20", [("10:00", 3, 1.0), ("11:00", 7, -0.5)], ("plan", "exited", "runner"))            # (3×1 − 7×0.5)/10＝−0.05（不是單純平均 0.25）
            + _trade("09:21", [("09:40", 10, -1.0)], ("plan", "exited", "stop"), target="t-stop")
            + _trade("09:22", [("09:50", 10, -0.3)], ("plan", "invalidated", "期貨跌破夜盤低點"), target="t-inv")
            + _trade("09:23", [("10:10", 10, 0.73)], ("override", "early_exit", "done"), target="t-ovr", engine="g12")
            + _trade("09:24", [], None, target="t-open"))
    _ingest(db, rows)
    db.execute("INSERT INTO verifications (date, hit_day) VALUES (?, 1)", (D,))
    s = build_trade_stats(db)
    g = next(x for x in s["groups"] if x["rule_version"] == "v3")
    assert g["r"]["n"] == 1 and g["r"]["avg_r"] == -0.05 and g["by_exit_reason"] == {"target": g["r"]}
    assert g["insight"]["hit"]["n"] == 1 and g["insight"]["hit"]["avg_r"] == -0.05 and g["playbook"] == "pullback_hold/p1"
    other = next(x for x in s["groups"] if x["rule_version"] is None)                                            # target 不在 DB
    assert other["r"]["n"] == 3 and sorted(other["by_exit_reason"]) == ["invalidated", "override", "stop"]
    assert other["by_exit_reason"]["stop"]["avg_r"] == -1.0 and other["by_exit_reason"]["invalidated"]["avg_r"] == -0.3
    assert other["override"]["with"]["n"] == 1 and other["override"]["with"]["avg_r"] == 0.73
    assert other["open_trades"] == 1 and other["mixed_versions"] is True and other["sufficient"] is False
    assert other["r"]["total_r"] == round(-1.0 - 0.3 + 0.73, 4) and other["r"]["max_drawdown_r"] == 1.3
    assert other["r"]["max_losing_streak"] == 2 and other["r"]["profit_factor"] == round(0.73 / 1.3, 4)
    assert other["costs"] == 3 * 2 * 12                                                                         # 3 筆已平：每筆 2 次成交 ×（10＋2）
    open_t = next(t for t in s["trades"] if t["target_id"] == "t-open")
    assert open_t["status"] == "open" and open_t["r"] is None and s["n_trades"] == 5


def test_reruns_and_run_modes_are_kept_apart(db):
    rows = (_trade("09:20", [("10:00", 10, 1.0)], ("plan", "exited", "runner"))
            + _trade("09:30", [("10:30", 10, -1.0)], ("plan", "exited", "stop"), run="-r2")                        # 同日重跑：另一筆
            + _trade("09:20", [("10:00", 10, 2.0)], ("plan", "exited", "runner"), mode="shioaji_sim"))
    _ingest(db, rows)
    trades = build_trades(rows)
    assert sorted((t["run_mode"], t["run"], t["r"]) for t in trades) == [("paper", 1, 1.0), ("paper", 2, -1.0), ("shioaji_sim", 1, 2.0)]
    s = build_trade_stats(db)
    by_mode = {g["run_mode"]: g for g in s["groups"]}
    assert by_mode["paper"]["r"]["n"] == 2 and by_mode["shioaji_sim"]["r"]["n"] == 1
    assert build_trade_stats(db, run_mode="shioaji_sim")["n_trades"] == 1


def test_inconsistent_fills_are_not_scored(db, caplog):
    rows = _trade("09:20", [("10:00", 12, 1.0)], ("plan", "exited", "runner"))                                  # 出比進多
    rows += _trade("09:21", [("10:00", 10, None)], ("plan", "exited", "stop"), target="t-nor")                  # 出場成交沒有 R
    _ingest(db, rows)
    s = build_trade_stats(db)
    assert all(t["status"] == "inconsistent" for t in s["trades"]) and all(g["r"]["n"] == 0 for g in s["groups"])
    assert sum(g["inconsistent_trades"] for g in s["groups"]) == 2 and "not scored" in caplog.text


def test_account_curves_take_the_latest_snapshot_per_day(db):
    snaps = _fixture("account_snapshots")
    day2 = {**snaps[0], "snapshot_id": "s-20260615-close", "as_of": "2026-06-15T13:50:00+08:00", "equity": 297400.0,
            "cash_balance": 297400.0, "available_margin": 297400.0}
    day3 = {**snaps[0], "snapshot_id": "s-20260616-close", "as_of": "2026-06-16T13:50:00+08:00", "equity": 303000.0,
            "cash_balance": 303000.0, "available_margin": 303000.0}
    snapshots_ingest(db, snaps + [day2, day3])
    a = build_trade_stats(db)["accounts"]
    assert len(a) == 1 and a[0]["days"] == 3 and a[0]["start_equity"] == 300412.0 and a[0]["end_equity"] == 303000.0
    assert a[0]["max_drawdown_pct"] == round((300412.0 - 297400.0) / 300412.0 * 100, 4)


def test_empty_db_is_fine(db):
    s = build_trade_stats(db)
    assert s["groups"] == [] and s["accounts"] == [] and s["trades"] == [] and s["min_sample"] == settings.STATS_MIN_SAMPLE


def test_api_and_stats_page(tmp_path):
    p = str(tmp_path / "t.db")
    conn = sqlite3.connect(p)
    create_all_tables(conn)
    target_put(conn, _fixture("targets")[0])
    _ingest(conn, _trade("09:20", [("10:00", 10, 1.0)], ("plan", "exited", "runner")))
    conn.commit()
    conn.close()
    c = TestClient(create_app(db_path=p, enable_scheduler=False))
    r = c.get("/api/trade-stats", params={"run_mode": "paper", "date_from": D, "date_to": D}).json()
    assert r["groups"][0]["r"]["avg_r"] == 1.0 and r["filters"]["run_mode"] == "paper"
    assert c.get("/api/trade-stats", params={"date_from": "2026-6-1"}).status_code == 422
    assert c.get("/api/trade-stats", params={"run_mode": "live"}).status_code == 422
    page = c.get("/stats").text
    assert "Paper／模擬交易成績" in page and "pullback_hold/p1" in page and "+1.00" in page
