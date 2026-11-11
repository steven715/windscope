"""integration/trade_stats（依成交打分，一筆交易＝一段部位）：分批出場的加權 R、風控平倉（成交沒有 target）、趨勢加碼／減碼被淨額、
同日重跑只看最後一次（跨日不切半）、日期區間不切半、到期沒成交、翻向、出場原因（停損／失效／目標／override／flatten）、
版本分組、Insight 對錯（開倉方向 vs 當天漲跌）、帳戶曲線（基準、含入出金）、/api/trade-stats 與 /stats 頁（壞了不 500）。"""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from config import settings
from db.events import events_ingest, snapshots_ingest
from db.instruments import instruments_seed
from db.schema import create_all_tables
from db.targets import target_put
from integration.trade_stats import build_trade_stats, exit_reason, latest_runs, system_exit_reason
from server.app import create_app

FIX = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "fixtures"
D, D2, D3 = "2026-06-12", "2026-06-15", "2026-06-16"
TGT = "0765ce4b0c35f0c0"                      # fixture：premarket_call、TAIFEX:TMF、做多（weight 0.6）、stop_pts 30、pullback_hold/p1


def _fixture(name: str) -> list[dict]:
    return [json.loads(x) for x in (FIX / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


_SEQ = [0]


def _ev(kind, action, ts, side=None, qty=None, price=None, r=None, reason=None, target=TGT, run="", mode="paper",
        engine="g13", day=D, order_reason=None):
    _SEQ[0] += 1
    det = ({"commission": 10, "tax": 2, "order_reason": order_reason} if kind == "fill" else None)
    return {"schema_version": "0.1", "event_id": f"sim-fut-01-e{_SEQ[0]:05d}{run}", "run_mode": mode, "account_id": "sim-fut-01",
            "strategy_id": "premarket_call" if target else None, "target_id": target, "instrument_id": "TAIFEX:TMF",
            "ts": f"{day}T{ts}:00+08:00", "recorded_at": f"{day}T{ts}:00+08:00", "kind": kind, "action": action, "side": side,
            "qty": qty, "price": price, "r_multiple": r, "reason": reason, "detail": det,
            "versions": {"execution_version": "e3", "risk_version": "r8", "engine_version": engine, "cost_model": "sim-c4/cost-c2"}}


def _fill(ts, side, qty, price, **kw):
    return _ev("fill", "fill", ts, side, qty, price, 0.0, **kw)


def _setup(conn):
    create_all_tables(conn)
    instruments_seed(conn)
    target_put(conn, _fixture("targets")[0])


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _setup(conn)
    yield conn
    conn.close()


def _ingest(conn, rows):
    res = events_ingest(conn, rows)
    assert res["invalid"] == 0, res["errors"][:2]


def _trades(db, **kw):
    return build_trade_stats(db, **kw)["trades"]


def test_reason_classifiers():
    assert exit_reason("override", "reduce", "x") == "override" and exit_reason("plan", "scaled_out", "+1R") == "target"
    assert exit_reason("plan", "invalidated", "期貨跌破") == "invalidated" and exit_reason("plan", "force_flat", "") == "force_flat"
    assert [exit_reason("plan", "exited", r) for r in ("stop", "runner", "scaled_out_all", "superseded by abc", "roll", "x")] == [
        "stop", "target", "target", "superseded", "roll", "other"]
    assert [system_exit_reason(r) for r in ("flatten", "roll", "orphan", "chase", None)] == ["flatten", "roll", "orphan", "system", "system"]


def test_scale_out_r_is_quantity_weighted_and_matches_tradewind_fill_r(db):
    """3 口 +1R（23030）、7 口 −0.5R（22985）→ (3×30 − 7×15)／(30×10)＝−0.05（不是單純平均 0.25）。"""
    _ingest(db, [_fill("09:20", "buy", 10, 23000.0), _ev("plan", "scaled_out", "09:59", "sell", 3, 23030.0, 1.0, "+1R"),
                 _fill("10:00", "sell", 3, 23030.0), _ev("plan", "exited", "10:59", "sell", 7, 22985.0, -0.5, "runner"),
                 _fill("11:00", "sell", 7, 22985.0)])
    s = build_trade_stats(db)
    (t,) = s["trades"]
    assert t["status"] == "closed" and t["r"] == -0.05 and t["exit_reason"] == "target" and t["playbook"] == "pullback_hold/p1"
    assert t["gross_pnl"] == (3 * 30 - 7 * 15) * 10 and t["costs"] == 36 and t["net_pnl"] == -150 - 36   # TMF 乘數 10
    g = s["groups"][0]
    assert g["r"]["avg_r"] == -0.05 and g["by_exit_reason"] == {"target": g["r"]} and g["sufficient"] is False


def test_risk_flatten_exit_without_target_closes_the_trade(db):
    """風控／kill switch 平倉單沒有 target（target_id 空、r_multiple 0）：一樣結束這筆，R 用價格算，原因＝flatten。"""
    _ingest(db, [_fill("09:20", "buy", 10, 23000.0), _fill("10:30", "sell", 10, 22946.0, target=None, order_reason="flatten")])
    (t,) = _trades(db)
    assert t["status"] == "closed" and t["r"] == -1.8 and t["exit_reason"] == "flatten" and t["target_id"] == TGT


def test_trend_add_and_reduce_netted_onto_the_new_target_stay_one_trade(db):
    """加碼：舊 target 歸零＋新 target 進場被淨額成一張掛在新 target 上的買單；減碼同理。都是同一段部位、以開倉 target 的 1R 計。"""
    _ingest(db, [_fill("09:20", "buy", 2, 23000.0), _fill("09:40", "buy", 2, 23010.0, target="t-new"),       # 加碼（掛新 target）
                 _fill("10:00", "sell", 1, 23020.0, target="t-new"),                                          # 減碼
                 _ev("plan", "exited", "10:59", "sell", 3, 23030.0, None, "stop", target="t-new"),
                 _fill("11:00", "sell", 3, 23030.0, target="t-new")])
    (t,) = _trades(db)
    assert t["status"] == "closed" and t["adds"] == 1 and t["entry_qty"] == 4 and t["targets"] == [TGT, "t-new"]
    assert t["r"] == round(((23020 - 23005) * 1 + (23030 - 23005) * 3) / (30 * 4), 4) and t["exit_reason"] == "stop"


def test_same_day_rerun_keeps_only_the_last_run_and_swing_across_a_rerun_is_one_trade(db):
    rows = [_fill("09:20", "buy", 10, 23000.0), _fill("09:40", "sell", 10, 22970.0, order_reason="flatten", target=None),  # 第 1 次（被取代）
            _fill("10:20", "buy", 10, 23000.0, run="-r2"),
            _fill("11:00", "sell", 10, 23060.0, run="-r2", target=None, order_reason="flatten"),
            _fill("09:20", "buy", 5, 23100.0, day=D2, target="t-swing"),                                       # 隔天進場
            _fill("10:00", "sell", 5, 23130.0, day=D3, run="-r2", target="t-swing")]                           # 再隔天重跑後出場
    _ingest(db, rows)
    assert len(latest_runs([{**r, "_session_date": r["ts"][:10]} for r in rows])) == 4
    ts = _trades(db)
    assert sorted((t["entry_ts"][:10], t["status"], t["r"]) for t in ts) == [(D, "closed", 2.0), (D2, "closed", None)]
    swing = next(t for t in ts if t["target_id"] == "t-swing")
    assert swing["unscored"] and swing["exit_ts"].startswith(D3)                     # t-swing 不在 DB：沒有 stop_pts → 不打分但不是未平倉


def test_date_window_does_not_cut_a_trade_in_half(db):
    _ingest(db, [_fill("13:00", "buy", 10, 23000.0, day=D), _fill("09:30", "sell", 10, 23030.0, day=D2)])
    assert [(t["status"], t["r"], t["entry_ts"][:10]) for t in _trades(db, date_from=D2, date_to=D2)] == [("closed", 1.0, D)]
    assert [t["status"] for t in _trades(db, date_from=D, date_to=D)] == ["open"]        # 只看到 6/12：那時還沒出場


def test_position_gone_without_an_exit_fill_is_not_scored(db):
    """到期結算（沒有出場成交）：之後的快照已經沒有部位 → closed_without_fill，不算未平倉、不打分。"""
    _ingest(db, [_fill("09:20", "buy", 10, 23000.0)])
    res = snapshots_ingest(db, [{**_fixture("account_snapshots")[0], "snapshot_id": "s-expiry-day", "as_of": f"{D}T13:50:00+08:00",
                                 "positions": []}])
    assert res["inserted"] == 1, res
    s = build_trade_stats(db)
    (t,) = s["trades"]
    assert t["status"] == "closed_without_fill" and t["r"] is None and s["groups"][0]["closed_without_fill"] == 1
    assert s["groups"][0]["open_trades"] == 0 and s["groups"][0]["r"]["n"] == 0


def test_flip_closes_and_opens_and_side_less_fills_are_skipped(db, caplog):
    bad = _fill("09:30", "buy", 3, 23000.0)
    bad["side"] = None
    _ingest(db, [_fill("09:20", "buy", 10, 23000.0), bad, _fill("10:00", "sell", 20, 23030.0), _fill("11:00", "buy", 10, 23000.0)])
    assert sorted((t["direction"], t["r"]) for t in _trades(db)) == [(-1, 1.0), (1, 1.0)] and "without side" in caplog.text


def test_override_reduce_to_zero_has_a_reason_and_nothing_crashes(db):
    """人為 reduce 到 0：只有 override/reduce、沒有 plan/exited——原因是 override；另一筆沒有出場事件＝other，排序不會炸。"""
    _ingest(db, [_fill("09:20", "buy", 10, 23000.0), _ev("override", "reduce", "09:59", "sell", 10, 23015.0, 0.5, "手動"),
                 _fill("10:00", "sell", 10, 23015.0),
                 _fill("10:20", "buy", 10, 23000.0, target="t-x"), _fill("10:40", "sell", 10, 22990.0, target="t-x")])
    s = build_trade_stats(db)
    assert {t["exit_reason"] for t in s["trades"]} == {"override", "other"}
    g = next(g for g in s["groups"] if g["rule_version"] == "v3")
    assert g["override"]["with"]["n"] == 1 and g["override"]["with"]["avg_r"] == 0.5


def test_versions_split_groups_and_insight_uses_direction_vs_actual_move(db):
    _ingest(db, [_fill("09:20", "buy", 10, 23000.0), _fill("10:00", "sell", 10, 23030.0),
                 _fill("10:20", "buy", 10, 23000.0, engine="g14"), _fill("10:40", "sell", 10, 22970.0, engine="g14")])
    db.execute("INSERT INTO verifications (date, day_change_class, hit_day) VALUES (?, 'up', 0)", (D,))  # hit_day 是重算後判斷的：不看它
    s = build_trade_stats(db)
    assert sorted(g["versions"]["engine_version"] for g in s["groups"]) == ["g13", "g14"]
    assert all(g["insight"]["hit"]["n"] == 1 and g["insight"]["miss"]["n"] == 0 for g in s["groups"])
    db.execute("UPDATE verifications SET day_change_class = 'flat'")
    assert all(g["insight"]["flat"]["n"] == 1 for g in build_trade_stats(db)["groups"])
    db.execute("UPDATE verifications SET day_change_class = 'down'")
    assert all(g["insight"]["miss"]["n"] == 1 for g in build_trade_stats(db)["groups"])


def test_run_modes_never_mix(db):
    _ingest(db, [_fill("09:20", "buy", 10, 23000.0), _fill("10:00", "sell", 10, 23030.0),
                 _fill("09:20", "buy", 10, 23000.0, mode="shioaji_sim"), _fill("10:00", "sell", 10, 22970.0, mode="shioaji_sim")])
    by = {g["run_mode"]: g["r"]["avg_r"] for g in build_trade_stats(db)["groups"]}
    assert by == {"paper": 1.0, "shioaji_sim": -1.0} and build_trade_stats(db, run_mode="shioaji_sim")["n_trades"] == 1


def test_account_curve_base_and_flags(db):
    s0 = _fixture("account_snapshots")[0]

    def snap(day, eq):
        return {**s0, "snapshot_id": f"s-{day}", "as_of": f"{day}T13:50:00+08:00", "equity": eq, "cash_balance": eq, "available_margin": eq}
    assert snapshots_ingest(db, [snap(D, 300000.0), snap(D2, 297000.0), snap(D3, 303000.0)])["inserted"] == 3
    (a,) = build_trade_stats(db)["accounts"]
    assert a["first_day_pnl_excluded"] and a["includes_cash_flows"] and a["start_equity"] == 300000.0 and a["days"] == 3
    (a,) = build_trade_stats(db, date_from=D2)["accounts"]                                  # 基準＝區間前最後一天（6/12 收盤）
    assert a["base_date"] == D and a["start_equity"] == 300000.0 and a["end_equity"] == 303000.0 and a["days"] == 2
    assert not a["first_day_pnl_excluded"] and a["max_drawdown_pct"] == 1.0 and a["return_pct"] == 1.0


def test_empty_db_is_fine(db):
    s = build_trade_stats(db)
    assert s["groups"] == [] and s["accounts"] == [] and s["trades"] == [] and s["min_sample"] == settings.STATS_MIN_SAMPLE


def test_api(tmp_path):
    p = str(tmp_path / "t.db")
    conn = sqlite3.connect(p)
    _setup(conn)
    _ingest(conn, [_fill("09:20", "buy", 10, 23000.0), _fill("10:00", "sell", 10, 23030.0)])
    conn.commit()
    conn.close()
    c = TestClient(create_app(db_path=p, enable_scheduler=False))
    r = c.get("/api/trade-stats", params={"run_mode": "paper", "date_from": D, "date_to": D}).json()
    assert r["groups"][0]["r"]["avg_r"] == 1.0 and r["filters"]["run_mode"] == "paper"
    assert c.get("/api/trade-stats", params={"date_from": "2026-6-1"}).status_code == 422
    assert c.get("/api/trade-stats", params={"run_mode": "live"}).status_code == 422
