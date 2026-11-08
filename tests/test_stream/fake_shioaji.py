"""假的 shioaji 模組（只有 ShioajiSource 用到的介面，形狀照 1.7.11 型別檔）：記錄 login／subscribe，測試自己呼叫 callback。"""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

DOC_TICKS = Path(__file__).resolve().parents[1] / "fixtures" / "shioaji" / "ticks_from_docs.jsonl"


class QuoteType:
    Tick = "tick"


class QuoteVersion:
    v1 = "v1"


def contract(code, delivery_month="", multiplier=10, exchange="TAIFEX"):
    return SimpleNamespace(code=code, delivery_month=delivery_month, multiplier=multiplier, exchange=exchange)


class _Category:
    def __init__(self, groups):
        self._groups = groups

    def get(self, key, default=None):
        return self._groups.get(key, default)


class FakeApi:
    def __init__(self, sdk, simulation=None):
        self.sdk, self.simulation = sdk, simulation
        self.subscribed, self.unsubscribed, self.logins, self.logged_out = [], [], [], 0
        self.Contracts = SimpleNamespace(Futures=_Category(sdk.futures), Stocks=_Category(sdk.stocks))
        self.fop = self.stk = self.event = self.down = None

    def login(self, api_key, secret_key, subscribe_trade=True):
        if self.sdk.fail_login:
            raise RuntimeError(f"login refused for key {api_key} secret {secret_key}")
        self.logins.append({"api_key": api_key, "subscribe_trade": subscribe_trade})

    def fetch_contracts(self, contract_download=False):
        if self.sdk.on_fetch:
            self.sdk.on_fetch()
        self.downloaded = contract_download

    def set_on_tick_fop_v1_callback(self, cb, bind=False):
        self.fop = cb

    def set_on_tick_stk_v1_callback(self, cb, bind=False):
        self.stk = cb

    def set_event_callback(self, cb):
        self.event = cb

    def set_session_down_callback(self, cb):
        self.down = cb

    def subscribe(self, contract, quote_type=None, intraday_odd=False, version=None):
        if contract.code in self.sdk.fail_subscribe:
            raise RuntimeError(f"subscribe {contract.code} timed out")
        self.subscribed.append((contract.code, quote_type, intraday_odd, version))

    def unsubscribe(self, contract, quote_type=None, intraday_odd=False, version=None):
        self.unsubscribed.append(contract.code)

    def logout(self):
        self.logged_out += 1

    def clear_on_tick_fop_v1_callback(self):
        self.fop = None

    def clear_on_tick_stk_v1_callback(self):
        self.stk = None

    def clear_event_callback(self):
        self.event = None

    def clear_session_down_callback(self):
        self.down = None


class FakeSdk:
    """`import shioaji as sdk` 的替身：Shioaji(simulation) 回 FakeApi；apis 記下每次登入建的 api。"""

    QuoteType = QuoteType
    QuoteVersion = QuoteVersion

    def __init__(self, futures=None, stocks=None):
        self.futures = futures if futures is not None else {}
        self.stocks = stocks if stocks is not None else {}
        self.apis: list[FakeApi] = []
        self.fail_login = False
        self.fail_subscribe: set[str] = set()
        self.on_fetch = None                     # 登入後、下載合約時呼叫（測試注入失敗或 stop）

    def Shioaji(self, simulation=None):
        api = FakeApi(self, simulation)
        self.apis.append(api)
        return api

    @property
    def api(self) -> FakeApi:
        return self.apis[-1]


def tick(code, ts, close, volume, **kw):
    """SDK tick 物件的替身：datetime 是 naive（文件範例是台北當地時間）。"""
    return SimpleNamespace(code=code, datetime=datetime.fromisoformat(ts), close=str(close), volume=volume,
                           simtrade=kw.pop("simtrade", False), **kw)


def doc_ticks() -> list[SimpleNamespace]:
    """官方文件的 tick 範例（fixture），轉成 SDK 物件的形狀。"""
    out = []
    for line in DOC_TICKS.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        r = json.loads(line)
        r["datetime"] = datetime.fromisoformat(f"{r.pop('date')}T{r.pop('time')}")
        r.pop("kind")
        out.append(SimpleNamespace(**r))
    return out
