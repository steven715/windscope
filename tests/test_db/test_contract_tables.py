"""契約表的唯一入口：instruments／bars（含 bars_v 映射）／targets／events／account_snapshots。"""

import json
import sqlite3
from pathlib import Path

import pytest

from db.bars import bars_query, bars_upsert, bars_latest_ts, normalize_ts
from db.events import events_ingest, events_list, ingest_inbox, snapshots_ingest, snapshots_list
from db.instruments import instrument_get, instrument_upsert, instruments_list, instruments_seed
from db.schema import create_all_tables
from db.targets import target_get, target_lock, target_put, targets_list
from utils.contract_schema import ContractError

FIX = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "fixtures"


def _fixture(name: str) -> list[dict]:
    return [json.loads(l) for l in (FIX / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    return conn


# ── instruments ──────────────────────────────────────────────────────────


def test_instruments_seed_list_get(db):
    n = instruments_seed(db)                       # config/instruments.json
    assert n >= 6
    allx = instruments_list(conn=db)
    assert [i["instrument_id"] for i in allx] == sorted(i["instrument_id"] for i in allx)
    assert {i["instrument_id"] for i in instruments_list(conn=db, tradable_only=True)} >= {"TAIFEX:TMF", "TWSE:2330"}
    assert "TWSE:TAIEX" not in {i["instrument_id"] for i in instruments_list(conn=db, tradable_only=True)}
    tmf = instrument_get("TAIFEX:TMF", conn=db)
    assert tmf["multiplier"] == 10 and tmf["initial_margin"] > 0
    assert instrument_get("TWSE:9999", conn=db) is None


def test_instruments_upsert_rejects_invalid(db):
    with pytest.raises(ContractError):
        instrument_upsert(db, {"instrument_id": "TWSE:0050", "market": "TWSE"})   # 缺必填
    assert instruments_list(conn=db) == []


# ── bars / bars_v ────────────────────────────────────────────────────────


def test_normalize_ts():
    assert normalize_ts("2026-06-12T14:30:12.123456") == "2026-06-12T14:30:12+08:00"
    assert normalize_ts("2026-06-12 14:30:12") == "2026-06-12T14:30:12+08:00"
    assert normalize_ts("2026-06-12T14:30:12+08:00") == "2026-06-12T14:30:12+08:00"
    assert normalize_ts("2026-06-12") == "2026-06-12T00:00:00+08:00"
    assert normalize_ts("2026-06-12T14:30") == "2026-06-12T14:30:00+08:00"
    # 其他時區一律換算成台北時間，不是硬貼 +08:00
    assert normalize_ts("2026-06-12T05:30:41Z") == "2026-06-12T13:30:41+08:00"
    assert normalize_ts("2026-06-12T05:30:41.5Z") == "2026-06-12T13:30:41+08:00"
    assert normalize_ts("2026-06-12T14:30:12-05:00") == "2026-06-13T03:30:12+08:00"
    assert normalize_ts("2026-06-12T14:30:12.123456+00:00") == "2026-06-12T22:30:12+08:00"
    assert normalize_ts("not a time") is None
    assert normalize_ts(None, fallback="2026-06-12T09:00:00+08:00") == "2026-06-12T09:00:00+08:00"
    assert normalize_ts(None) is None


def test_bars_view_maps_legacy_tables_to_contract(db):
    """三張舊 raw 表經 bars_v 讀出來就是契約 bars 的形狀，且通過 schema。"""
    from utils.contract_schema import validate_record

    db.execute("INSERT INTO raw_index (date, open, high, low, close, collected_at) "
               "VALUES ('2026-06-12', 22890.12, 23010.55, 22850, 22980.33, '2026-06-12T14:30:12.5')")
    db.execute("INSERT INTO raw_stock_daily (date, stock_id, open, high, low, close, volume, collected_at) "
               "VALUES ('2026-06-12', '2330', 985, 992, 980, 990, 28431000, '2026-06-12 18:31:04')")
    db.execute("INSERT INTO raw_futures (date, night_close, night_volume, oi_net_foreign, collected_at) "
               "VALUES ('2026-06-15', 23105, 41230, -32150, '2026-06-15T05:30:41')")
    idx = bars_query(db, "TWSE:TAIEX")
    assert len(idx) == 1 and idx[0]["session_date"] == "2026-06-12"
    assert idx[0]["published_at"] == "2026-06-12T13:30:00+08:00"
    assert idx[0]["ingested_at"] == "2026-06-12T14:30:12+08:00"
    assert validate_record("bars", idx[0]) == []
    stk = bars_query(db, "TWSE:2330")[0]
    assert stk["volume"] == 28431000 and validate_record("bars", stk) == []
    fut = bars_query(db, "TAIFEX:TX", session="night")[0]
    assert fut["session_date"] == "2026-06-15" and fut["ts"] == "2026-06-14T15:00:00+08:00"
    assert fut["published_at"] == "2026-06-15T05:00:00+08:00"
    assert fut["open_interest"] is None and fut["volume"] == 41230   # oi_net_foreign 是帶號淨部位，不映射
    assert validate_record("bars", fut) == []
    assert bars_query(db, "TWSE:TAIEX", date_from="2026-06-13") == []


def test_bars_upsert_revision_and_latest(db):
    rows = [b for b in _fixture("bars") if b["instrument_id"] == "TAIFEX:TMF"]
    assert bars_upsert(db, rows) == 2
    newer = {**rows[1], "close": 23019.0, "revision": 1}
    bars_upsert(db, [newer])
    got = bars_query(db, "TAIFEX:TMF", "1m")
    assert len(got) == 1 and got[0]["close"] == 23019.0 and got[0]["revision"] == 1   # 取最大 revision
    assert bars_latest_ts(db, "TAIFEX:TMF", "1m") == rows[1]["ts"]
    assert bars_latest_ts(db, "TAIFEX:TMF", "5m") is None
    with pytest.raises(ContractError):
        bars_upsert(db, [{**rows[0], "resolution": "2m"}])


def test_bars_query_dedupes_before_paging_and_prefers_bars_table(db):
    """同鍵多 revision 先去重再 LIMIT/OFFSET；bars 表有同日資料時 raw 映射列被排除。"""
    base = [b for b in _fixture("bars") if b["resolution"] == "1m"][0]
    recs = []
    for i in range(3):
        ts = f"2026-06-12T09:2{i}:00+08:00"
        pub = f"2026-06-12T09:2{i + 1}:00+08:00"
        for rev in range(3):
            recs.append({**base, "ts": ts, "published_at": pub, "revision": rev, "close": 23000 + rev})
    bars_upsert(db, recs)
    got = bars_query(db, "TAIFEX:TMF", "1m", limit=2)
    assert [g["revision"] for g in got] == [2, 2] and len(got) == 2
    assert bars_query(db, "TAIFEX:TMF", "1m", limit=2, offset=2)[0]["ts"] == "2026-06-12T09:22:00+08:00"
    # raw_index 的 2026-06-12 與 bars 表同日同鍵 → 只剩 bars 表那筆
    db.execute("INSERT INTO raw_index (date, open, high, low, close, collected_at) "
               "VALUES ('2026-06-12', 1, 2, 0.5, 1.5, '2026-06-12T14:30:12')")
    assert bars_query(db, "TWSE:TAIEX")[0]["source"] == "twse.raw_index"
    bars_upsert(db, [{**base, "instrument_id": "TWSE:TAIEX", "resolution": "1d", "session": "regular",
                      "ts": "2026-06-12T09:00:00+08:00", "published_at": "2026-06-12T13:30:00+08:00",
                      "source": "twse.MI_5MINS_HIST", "volume": None, "revision": 0}])
    out = bars_query(db, "TWSE:TAIEX")
    assert len(out) == 1 and out[0]["source"] == "twse.MI_5MINS_HIST"


# ── targets ──────────────────────────────────────────────────────────────


def test_targets_put_get_list_lock(db):
    rows = [{**r, "locked_at": None} for r in _fixture("targets")]
    for r in rows:
        target_put(db, r)
    assert target_get(db, rows[0]["target_id"]) == rows[0]
    assert len(targets_list(db, session_date="2026-06-12")) == 1
    assert len(targets_list(db, strategy_id="trend_signal")) == 2
    assert targets_list(db, since="2999-01-01T00:00:00+08:00") == []          # since＝有變動的時刻
    assert len(targets_list(db, since="2000-01-01T00:00:00+08:00")) == len(rows)
    # 鎖定：寫 locked_at；再鎖不變；重 put（重算）不解除
    unlocked = rows[0]
    assert target_get(db, rows[0]["target_id"])["locked_at"] is None
    first = target_lock(db, rows[0]["target_id"], "2026-06-12T08:48:00+08:00")["locked_at"]
    assert first == "2026-06-12T08:48:00+08:00"
    assert target_lock(db, rows[0]["target_id"], "2026-06-12T08:50:00+08:00")["locked_at"] == first
    target_put(db, unlocked)                                           # 內容相同：保留鎖定
    assert target_get(db, rows[0]["target_id"])["locked_at"] == first
    assert target_lock(db, "nope") is None


def test_locked_target_rejects_content_change_unless_forced(db):
    from db.targets import TargetLockedError

    rec = {**_fixture("targets")[0], "locked_at": None}
    target_put(db, rec)
    target_lock(db, rec["target_id"], "2026-06-12T08:48:00+08:00")
    changed = {**rec, "target_weight": -1.0}
    with pytest.raises(TargetLockedError):
        target_put(db, changed)
    assert target_get(db, rec["target_id"])["target_weight"] == rec["target_weight"]
    target_put(db, changed, force=True)
    got = target_get(db, rec["target_id"])
    assert got["target_weight"] == -1.0 and got["locked_at"] == "2026-06-12T08:48:00+08:00"


def test_targets_since_sees_locks(db):
    """since 是「有變動」：鎖定後用更晚的 since 仍拿得到那筆（tradewind 輪詢的主流程）。"""
    rec = {**_fixture("targets")[0], "locked_at": None}
    target_put(db, rec)
    db.execute("UPDATE targets SET updated_at = '2000-01-01T00:00:00+08:00'")   # 模擬很久以前產生
    assert targets_list(db, since="2020-01-01T00:00:00+08:00") == []
    target_lock(db, rec["target_id"], "2026-06-12T08:52:00+08:00")                 # 鎖定＝一次變動
    assert [t["target_id"] for t in targets_list(db, since="2020-01-01T00:00:00+08:00")] == [rec["target_id"]]
    assert targets_list(db, locked_only=True)[0]["locked_at"] == "2026-06-12T08:52:00+08:00"


def test_targets_put_rejects_invalid(db):
    bad = {**_fixture("targets")[0], "target_weight": 2}
    with pytest.raises(ContractError):
        target_put(db, bad)
    assert targets_list(db) == []


# ── events / account_snapshots ───────────────────────────────────────────


def test_events_ingest_idempotent_and_invalid(db):
    ev = _fixture("events")
    r = events_ingest(db, ev)
    assert r["inserted"] == len(ev) and r["duplicates"] == 0 and r["invalid"] == 0
    r2 = events_ingest(db, ev + [{"event_id": "bad"}, 123, None, ["x"]])
    assert r2["inserted"] == 0 and r2["duplicates"] == len(ev) and r2["invalid"] == 4 and len(r2["errors"]) == 4
    # run_mode 與來源不符 → invalid（Paper 與實盤永不混算）
    r3 = events_ingest(db, [{**ev[0], "event_id": "z-00000001"}], expect_run_mode="shioaji_live")
    assert r3["invalid"] == 1 and "run_mode" in r3["errors"][0]
    assert len(events_list(db, session_date="2026-06-12", run_mode="paper")) == len(ev)
    assert [e["kind"] for e in events_list(db, target_id=ev[0]["target_id"])][:2] == ["plan", "plan"]
    assert events_list(db, run_mode="shioaji_live") == []


def test_snapshots_ingest_and_list(db):
    sn = _fixture("account_snapshots")
    assert snapshots_ingest(db, sn)["inserted"] == 2
    assert snapshots_ingest(db, sn)["duplicates"] == 2
    got = snapshots_list(db, session_date="2026-06-12")
    assert [s["as_of"] for s in got] == sorted(s["as_of"] for s in sn)      # as_of 升冪


def test_ingest_inbox_scans_files_idempotently(db, tmp_path):
    ev, sn = _fixture("events"), _fixture("account_snapshots")
    (tmp_path / "events" / "paper").mkdir(parents=True)
    (tmp_path / "account_snapshots" / "paper").mkdir(parents=True)
    (tmp_path / "events" / "paper" / "2026-06-12.jsonl").write_text(
        "\n".join(json.dumps(e) for e in ev) + "\nnot json\n", encoding="utf-8")
    (tmp_path / "account_snapshots" / "paper" / "2026-06-12.jsonl").write_text(
        "\n".join(json.dumps(s) for s in sn), encoding="utf-8")
    (tmp_path / "events" / "paper" / "2026-06-13.jsonl").write_text(
        json.dumps({**ev[0], "event_id": "x-20260613-0001", "ts": "2026-06-13T09:00:00+08:00"}), encoding="utf-8")

    s1 = ingest_inbox(db, str(tmp_path), session_date="2026-06-12")
    assert s1["files"] == 2 and s1["events"]["inserted"] == len(ev) and s1["account_snapshots"]["inserted"] == 2
    s2 = ingest_inbox(db, str(tmp_path))                     # 全掃：舊檔全部 duplicates，新檔 1 筆
    assert s2["files"] == 3 and s2["events"]["inserted"] == 1 and s2["events"]["duplicates"] == len(ev)
    assert ingest_inbox(db, str(tmp_path / "missing"))["files"] == 0
    # 歸屬交易日取檔名、run_mode 取目錄：放錯目錄的檔整批 invalid，且不影響其他檔
    (tmp_path / "events" / "shioaji_live").mkdir()
    (tmp_path / "events" / "shioaji_live" / "2026-06-14.jsonl").write_text(
        json.dumps({**ev[0], "event_id": "live-0001"}) + "\n[1,2]\n", encoding="utf-8")
    s3 = ingest_inbox(db, str(tmp_path), session_date="2026-06-14")
    assert s3["files"] == 1 and s3["events"]["inserted"] == 0 and s3["events"]["invalid"] == 2
    assert events_list(db, run_mode="shioaji_live") == []
    assert events_list(db, session_date="2026-06-13")[0]["event_id"] == "x-20260613-0001"
