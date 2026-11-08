"""ShioajiSource（假的 SDK，不打網路）：登入只要行情、訂閱近月＋下一口、依交易日的近月路由連續代號（換月日收盤後的夜盤是次月）、
月份合約與現股直接路由、試撮／暫停／零股丟掉、整股張→股、callback 參數個數兩種都吃、主檔對不上不訂、斷線重連、換日重訂、
沉默警告、金鑰遮罩；接上 StreamRunner 合成 1m 並落地正式 DB。"""

import sqlite3
import sys
from pathlib import Path

import pytest

from collectors.market_stream import StreamRunner
from collectors.shioaji_source import ShioajiSource
from config import settings
from db.bars import bars_query
from db.instruments import instrument_get, instrument_upsert, instruments_seed
from db.schema import create_all_tables
from server.stream import StreamHub
from tests.test_stream.fake_shioaji import FakeSdk, contract, doc_ticks, tick

KEY, SECRET = "key-abc123", "secret-xyz789"


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    """主檔：微台連續（換月日＝到期前 1 個交易日）＋ 06、07 月合約＋台積電。"""
    p = str(tmp_path / "t.db")
    conn = sqlite3.connect(p)
    create_all_tables(conn)
    instruments_seed(conn)
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "DB_PATH", p)
    cont = {**instrument_get("TAIFEX:TMF", db_path=p), "roll_rule": "days_before_expiry:1"}
    jul = {**instrument_get("TAIFEX:TMF202606", db_path=p), "instrument_id": "TAIFEX:TMF202607", "symbol": "TMF202607",
           "name": "微型臺指期 2026/07", "expiry": "2026-07-15"}
    conn = sqlite3.connect(p)
    instrument_upsert(conn, cont)
    instrument_upsert(conn, jul)
    conn.commit()
    conn.close()
    return p


def _sdk() -> FakeSdk:
    return FakeSdk(futures={"TMF": [contract("TMFR1", "202606"), contract("TMFF6", "202606"), contract("TMFG6", "202607")]},
                   stocks={"2330": contract("2330", exchange="TSE")})


class _Clock:
    def __init__(self, t: str) -> None:
        self.t = t

    def __call__(self) -> str:
        return self.t


def _source(db, sdk, instruments=("TAIFEX:TMF",), t="2026-06-12T09:00:00+08:00", **kw):
    clock = _Clock(t)
    src = ShioajiSource(list(instruments), db_path=db, sdk=sdk, api_key=KEY, secret=SECRET, holidays=frozenset, clock=clock, **kw)
    got = []
    ok = src.start(lambda *a: got.append(a), threaded=False)
    return src, got, ok, clock


def test_login_is_market_data_only_and_subscribes_front_plus_next_month(db):
    sdk = _sdk()
    src, _, ok, _ = _source(db, sdk)
    api = sdk.api
    assert ok and api.simulation is True and api.logins == [{"api_key": KEY, "subscribe_trade": False}] and api.downloaded
    assert sorted(api.subscribed) == [("TMFF6", "tick", False, "v1"), ("TMFG6", "tick", False, "v1")]   # 不訂永豐的 R1
    src.stop()
    assert api.logged_out == 1 and sorted(api.unsubscribed) == ["TMFF6", "TMFG6"]


def test_continuous_follows_the_roll_rule_front_month_and_night_session_belongs_to_the_next_day(db):
    """換月日 06-16（到期 06-17 前 1 個交易日）：日盤仍是 06 月；06-16 15:00 起的夜盤歸 06-17 → 07 月。"""
    sdk = _sdk()
    src, got, _, _ = _source(db, sdk, t="2026-06-16T09:00:00+08:00")
    cb = sdk.api.fop
    cb(tick("TMFF6", "2026-06-16T10:00:01.250000", 40100, 2))                # 06 月、日盤 → 連續
    cb(tick("TMFG6", "2026-06-16T10:00:02", 40150, 1))                       # 07 月、日盤 → 不是近月，不送
    cb(tick("TMFF6", "2026-06-16T15:00:05", 40090, 1))                       # 06 月、夜盤（歸 06-17）→ 不送
    cb(tick("TMFG6", "2026-06-16T15:00:06", 40160, 3))                       # 07 月、夜盤 → 連續
    src.pump()
    assert got == [("TAIFEX:TMF", "2026-06-16T10:00:01+08:00", 40100.0, 2), ("TAIFEX:TMF", "2026-06-16T15:00:06+08:00", 40160.0, 3)]
    assert src.skipped == 2


def test_dated_contracts_and_stocks_route_directly_and_stock_lots_become_shares(db):
    sdk = _sdk()
    src, got, ok, _ = _source(db, sdk, instruments=("TAIFEX:TMF202607", "TWSE:2330"))
    assert ok and sorted(c for c, *_ in sdk.api.subscribed) == ["2330", "TMFG6"]
    sdk.api.fop(tick("TMFG6", "2026-06-12T09:30:00", 40150, 1))
    sdk.api.stk("TSE", tick("2330", "2026-06-12T09:30:01", "1005.0", 3, suspend=False, intraday_odd=False))   # 舊版 (exchange, tick)
    src.pump()
    assert got == [("TAIFEX:TMF202607", "2026-06-12T09:30:00+08:00", 40150.0, 1), ("TWSE:2330", "2026-06-12T09:30:01+08:00", 1005.0, 3000)]


def test_trial_match_suspended_and_odd_lot_ticks_are_dropped(db):
    sdk = _sdk()
    src, got, _, _ = _source(db, sdk, instruments=("TWSE:2330", "TAIFEX:TMF"))
    sdk.api.fop(tick("TMFF6", "2026-06-12T13:30:00", 40000, 5, simtrade=True))           # 試撮（盤中時刻也丟）
    sdk.api.stk(tick("2330", "2026-06-12T09:30:00", 1000, 1, suspend=True, intraday_odd=False))
    sdk.api.stk(tick("2330", "2026-06-12T09:30:00", 1000, 50, suspend=False, intraday_odd=True))   # 盤中零股
    sdk.api.fop(tick("XXXX", "2026-06-12T09:30:00", 1, 1))                               # 沒訂的代號
    src.pump()
    assert got == [] and src.skipped == 4


def test_documented_tick_shapes_parse(db):
    """官方文件的 tick 範例（fixture）：close 是字串、time 帶微秒；期貨量＝口、整股量＝張；零股丟掉。"""
    sdk = FakeSdk(futures={"TMF": [contract("TXFF6", "202606"), contract("TMFG6", "202607")]},
                  stocks={"2330": contract("2890", exchange="TSE")})
    src, got, _, _ = _source(db, sdk, instruments=("TAIFEX:TMF202606",))
    for t in doc_ticks():
        if t.code == "TXFF6":
            sdk.api.fop(t)
    src.pump()
    assert got == [("TAIFEX:TMF202606", "2026-05-20T18:50:02+08:00", 40619.0, 2),
                   ("TAIFEX:TMF202606", "2026-05-20T18:54:43+08:00", 40608.0, 1)]
    odd = [t for t in doc_ticks() if t.code == "2890"]
    assert [t.intraday_odd for t in odd] == [False, True] and odd[0].volume == 5


def test_master_mismatches_are_not_subscribed(db, caplog):
    two = FakeSdk(futures={"TMF": [contract("TMFF6", "202606"), contract("TMFF6X", "202606"), contract("TMFG6", "202607")]})
    src, _, ok, _ = _source(db, two, instruments=("TAIFEX:TMF202606",))
    assert not ok and "expected exactly one" in caplog.text                  # 兩口對得上：不猜
    mult = FakeSdk(futures={"TMF": [contract("TMFF6", "202606", multiplier=50), contract("TMFG6", "202607")]})
    src, _, ok, _ = _source(db, mult, instruments=("TAIFEX:TMF",))
    assert ok and [c for c, *_ in mult.api.subscribed] == ["TMFG6"] and "multiplier 50 != master 10" in caplog.text
    otc = FakeSdk(stocks={"2330": contract("2330", exchange="OTC")})
    src, _, ok, _ = _source(db, otc, instruments=("TWSE:2330", "TAIFEX:NOPE"))
    assert not ok and "want TSE" in caplog.text and "TAIFEX:NOPE is not in the instruments master" in caplog.text


def test_missing_next_month_is_reported_and_after_the_last_roll_nothing_is_subscribed(db, caplog):
    sdk = _sdk()
    src, _, ok, _ = _source(db, sdk, t="2026-07-10T09:00:00+08:00")         # 06 月已換掉：近月 07，主檔沒有 08
    assert ok and [c for c, *_ in sdk.api.subscribed] == ["TMFG6"] and "no month after TAIFEX:TMF202607" in caplog.text
    src, _, ok, _ = _source(db, _sdk(), t="2026-07-15T09:00:00+08:00")      # 07 月換月日（07-14）也過了
    assert not ok and "add the current and next month" in caplog.text


def test_new_trading_day_resubscribes_and_drops_the_expired_month(db):
    sdk = _sdk()
    src, _, _, clock = _source(db, sdk, t="2026-06-16T13:30:00+08:00")
    assert sorted(c for c, *_ in sdk.api.subscribed) == ["TMFF6", "TMFG6"]
    clock.t = "2026-06-16T15:01:00+08:00"                                     # 夜盤開了：交易日 06-17，近月 07
    src._housekeeping()
    assert sdk.api.unsubscribed == ["TMFF6"]


def test_session_down_reconnects_with_a_fresh_login_and_backs_off_on_failure(db, caplog):
    sdk = _sdk()
    src, got, _, _ = _source(db, sdk)
    first = sdk.api
    first.down()
    sdk.fail_login = True
    src._housekeeping()
    assert len(sdk.apis) == 2 and src.reconnects == 1 and "reconnect failed" in caplog.text
    assert SECRET not in caplog.text and KEY not in caplog.text                  # 錯誤訊息遮掉金鑰
    sdk.fail_login = False
    src._next_retry = 0
    src._housekeeping()
    assert len(sdk.apis) == 3 and sorted(c for c, *_ in sdk.api.subscribed) == ["TMFF6", "TMFG6"] and not src._down.is_set()
    sdk.api.fop(tick("TMFF6", "2026-06-12T09:30:00", 40100, 1))
    src.pump()
    assert got[-1][0] == "TAIFEX:TMF"


def test_login_cap_per_day(db, caplog, monkeypatch):
    monkeypatch.setattr(settings, "SHIOAJI_MAX_LOGINS_PER_DAY", 1)
    sdk = _sdk()
    src, _, ok, _ = _source(db, sdk)
    src._down.set()
    src._housekeeping()
    assert ok and len(sdk.apis) == 1 and "logins today already" in caplog.text


def test_silence_during_the_session_warns_once(db, caplog, monkeypatch):
    import collectors.shioaji_source as mod
    mono = {"t": 1000.0}
    monkeypatch.setattr(mod.time, "monotonic", lambda: mono["t"])
    src, _, _, clock = _source(db, _sdk(), t="2026-06-12T10:00:00+08:00")
    src._check_silence()
    mono["t"] += settings.SHIOAJI_SILENCE_WARN_SECONDS + 1
    src._check_silence()
    src._check_silence()
    assert caplog.text.count("no tick for") == 1
    clock.t = "2026-06-12T14:00:00+08:00"                                     # 收盤後：不警告、重設
    mono["t"] += 1000
    src._check_silence()
    assert caplog.text.count("no tick for") == 1


def test_missing_keys_or_package_do_not_start(db, monkeypatch, caplog):
    assert ShioajiSource(["TAIFEX:TMF"], db_path=db, sdk=_sdk(), api_key="", secret=SECRET).start(lambda *a: None) is False
    monkeypatch.setitem(sys.modules, "shioaji", None)                        # 沒裝套件
    assert ShioajiSource(["TAIFEX:TMF"], db_path=db, api_key=KEY, secret=SECRET).start(lambda *a: None) is False
    assert "not installed" in caplog.text


def test_end_to_end_with_the_runner_builds_1m_bars_into_the_main_db(db, monkeypatch):
    monkeypatch.setattr("collectors.market_stream.now_tw_iso", lambda: "2026-06-12T09:00:59+08:00")   # 即時：tick 在當下到
    sdk = _sdk()
    hub = StreamHub()
    clock = _Clock("2026-06-12T09:00:00+08:00")
    src = ShioajiSource(["TAIFEX:TMF"], db_path=db, sdk=sdk, api_key=KEY, secret=SECRET, holidays=frozenset, clock=clock)
    runner = StreamRunner(src, hub, db, instruments=["TAIFEX:TMF"], clock_flush=False)
    assert runner.start() and not runner.replay and runner.persist_db == db
    for ts, px in (("2026-06-12T09:00:01", 40100), ("2026-06-12T09:00:30", 40120), ("2026-06-12T09:01:02", 40110)):
        sdk.api.fop(tick("TMFF6", ts, px, 1))
    src.pump()                                                               # worker 跑的那一步
    runner.stop(now="2026-06-12T09:03:00+08:00")
    conn = sqlite3.connect(db)
    bars = bars_query(conn, "TAIFEX:TMF", "1m", "2026-06-12", "2026-06-12")
    conn.close()
    assert [(b["ts"], b["open"], b["high"], b["close"], b["volume"]) for b in bars][:1] == [
        ("2026-06-12T09:00:00+08:00", 40100.0, 40120.0, 40120.0, 2)]
    assert runner.ticks == 3 and runner.persisted == 2
