"""永豐 Shioaji 即時行情來源（MARKET_SOURCE=shioaji）：訂閱逐筆成交 → (instrument_id, ts, price, volume) 交給 StreamRunner。

API 事實的來源（不猜）：shioaji 1.7.11 套件自帶的型別檔 `_core.pyi`（tradewind 釘的同一版）＋官方文件
（sinotrade.github.io「即時行情／期貨選擇權」「即時行情／證券」，2026-11-08 讀）。查不到的標 ⚠，列在 docs/data_sources.md 的本機驗證清單。

- **登入**：`Shioaji(simulation=SHIOAJI_SIMULATION)` → `login(api_key, secret_key, subscribe_trade=False)` → `fetch_contracts(contract_download=True)`。
  只要行情權限的 key，不需憑證（憑證只有下單才要）；windscope 不下單。金鑰只從 settings（環境變數）讀，錯誤訊息遮掉。
- **合約**（主檔 instruments 是單一真相，不從永豐的合約表新增商品）：
  - 月份合約 `TAIFEX:TMF202606` → 商品根 `TMF`＋交割月 `202606` → `Contracts.Futures.get("TMF")` 裡 delivery_month 相符、不是連續月代號
    （R1／R2）的那一口；要剛好一口、乘數與主檔相同，否則不訂（記 ERROR）。
  - 連續代號 `TAIFEX:TMF`（主檔 expiry 空、有 roll_rule）：**不用永豐的 R1**（R1 在到期日才換，tradewind 的連續代號在換月日之後就是次月）。
    依 `strategylib.roll.front_contract` 的近月，加上下一口，兩口都訂；每筆 tick 先算它歸哪個交易日（夜盤歸下一個交易日），
    那天的近月是這一口才當成連續代號的 tick。所以換月日收盤後的夜盤就是次月，與 tradewind 交易的那口一致。
  - 現股 `TWSE:2330`／`TPEX:xxxx` → `Contracts.Stocks.get("2330")`，exchange 要對得上（TWSE↔TSE、TPEX↔OTC）。只訂整股；盤中零股另一條行情，
    價格與整股不同，混進同一條 1 分 K 會錯（要的話契約另開商品）。
- **tick**（`TickFOPv1`／`TickSTKv1`）：`simtrade=True`（試撮）、`suspend=True`（暫停交易）、`intraday_odd=True` 一律丟；
  時刻用 `datetime`（文件範例是台北當地時間，naive 視為 Asia/Taipei ⚠，runner 再正規化一次）；價格＝`close`；
  量：期貨＝口（文件 lot）；現股整股＝張 → ×1000 成股（契約的現股量是股）。
- **執行緒**：SDK 的 callback 只把 tick 丟進有界佇列；本模組的 worker 執行緒取出、轉換、交給 runner（runner 有自己的鎖）。
  callback 的參數個數依 SDK 版本不同（1.7.11 型別檔是 `(tick)`，舊文件是 `(exchange, tick)`）：一律取最後一個。
- **斷線**：`session_down` callback → worker 依 SHIOAJI_RECONNECT_BACKOFF 倍增重登、重訂（一天最多 SHIOAJI_MAX_LOGINS_PER_DAY 次）；
  盤中 SHIOAJI_SILENCE_WARN_SECONDS 秒沒有任何 tick 記 WARNING（每段沉默一次）。**不輪詢 snapshot**（官方明文禁止盤中輪詢，會封帳號與 IP）。
- **每天重新規劃**：交易日變了就重算要訂哪幾口（近月＋下一口），退掉不用的、補訂新的；主檔沒有下一口記 ERROR（補主檔，不猜）。
"""

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from config import settings
from db.bars import normalize_ts
from db.instruments import instruments_list
from integration.market_sessions import attribute
from strategylib.roll import dated_contracts, front_contract, is_continuous
from utils.trading_calendar import get_next_trading_day, holiday_dates, is_trading_day
from utils.tw_time import now_tw_iso

logger = logging.getLogger(__name__)

_MONTH = re.compile(r"^(?P<root>[A-Z]+)(?P<month>\d{6})$")
_CONTINUOUS_CODE = re.compile(r"R\d$")                       # 永豐的連續月代號（TXFR1、TMFR2）
_STOCK_EXCHANGE = {"TWSE": "TSE", "TPEX": "OTC"}
_QUEUE_MAX = 100_000
SHARES_PER_LOT = 1000                                        # 整股 1 張＝1000 股（tick 的整股量單位是張）


def _v(x) -> str:
    """enum 或字串 → 字串值（SDK 的 enum 有 .value，假的 SDK 直接給字串）。"""
    return str(getattr(x, "value", x) if x is not None else "")


def _scrub(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


@dataclass
class _Sub:
    """一個訂閱：永豐合約與它餵給哪些 windscope 商品。"""

    code: str
    contract: object
    kind: str                                    # future／stock
    master_id: str                               # 這口在主檔的 id（月份合約或現股）
    direct: set[str] = field(default_factory=set)        # 直接對應的商品（月份合約本身、現股）
    continuous: set[str] = field(default_factory=set)    # 依近月路由的連續代號


class ShioajiSource:
    """永豐行情來源（見模組說明）。sdk／clock 可注入（測試用假的 SDK，不打網路）。"""

    name = "shioaji"

    def __init__(self, instruments: list[str] | None = None, db_path: str | None = None, sdk=None,
                 simulation: bool | None = None, api_key: str | None = None, secret: str | None = None,
                 holidays: Callable[[], frozenset[str]] | None = None, clock: Callable[[], str] = now_tw_iso) -> None:
        self.instruments = list(instruments if instruments is not None else settings.STREAM_INSTRUMENTS)
        self.db_path = db_path
        self._sdk = sdk
        self.simulation = settings.SHIOAJI_SIMULATION if simulation is None else simulation
        self._key = settings.SHIOAJI_API_KEY if api_key is None else api_key
        self._secret = settings.SHIOAJI_SECRET if secret is None else secret
        self._holidays = holidays or holiday_dates
        self._clock = clock
        self._api = None
        self._on_tick = None
        self._master: dict[str, dict] = {}
        self._subs: dict[str, _Sub] = {}                         # code → 訂閱
        self._plan_date: str | None = None
        self._q: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._pump_lock = threading.Lock()                              # 一次一個取用者：tick 依到達順序交出去
        self._stop = threading.Event()
        self._down = threading.Event()
        self._worker: threading.Thread | None = None
        self._logins: dict[str, int] = {}
        self._backoff = settings.SHIOAJI_RECONNECT_BACKOFF[0]
        self._next_retry = 0.0
        self._last_tick_wall: float | None = None
        self._silence_warned = False
        self.received = 0
        self.forwarded = 0
        self.skipped = 0
        self.overflow = 0
        self.reconnects = 0

    # ── 啟動／停止 ────────────────────────────────────────────────
    def start(self, on_tick, on_bar=None, threaded: bool = True) -> bool:
        """登入、訂閱、開 worker；任何失敗記 ERROR 回 False（server 照常）。threaded=False 不開 worker（測試用 pump()）。"""
        if not self._key or not self._secret:
            logger.error("shioaji source: SHIOAJI_API_KEY / SHIOAJI_SECRET are not set; not started")
            return False
        if self._sdk is None:
            try:
                import shioaji as sdk                                # 選用套件：只有 MARKET_SOURCE=shioaji 才載入
            except ImportError:
                logger.error("shioaji source: the shioaji package is not installed (pip install shioaji==1.7.11); not started")
                return False
            self._sdk = sdk
        self._on_tick = on_tick
        try:
            self._master = {r["instrument_id"]: r for r in instruments_list(db_path=self.db_path)}
        except Exception as e:
            logger.error("shioaji source: instruments master unreadable: %s; not started", e)
            return False
        if not self._connect():
            return False
        if not self._subs:
            logger.error("shioaji source: nothing subscribed for %s; not started", self.instruments)
            self._logout()
            return False
        if threaded:
            self._worker = threading.Thread(target=self._run, daemon=True, name="shioaji-source")
            self._worker.start()
        return True

    def stop(self) -> None:
        """停 worker、退訂、登出（錯誤只記錄）。"""
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=5)
        self.pump()                                                   # 手上已收到的照常交出去
        for sub in list(self._subs.values()):
            self._unsubscribe(sub)
        self._subs.clear()
        self._logout()
        logger.info("shioaji source stop: received %d, forwarded %d, skipped %d, overflow %d, reconnects %d",
                    self.received, self.forwarded, self.skipped, self.overflow, self.reconnects)

    def _connect(self) -> bool:
        day = self._clock()[:10]
        if self._logins.get(day, 0) >= settings.SHIOAJI_MAX_LOGINS_PER_DAY:
            logger.error("shioaji source: %d logins today already; not logging in again until tomorrow", self._logins[day])
            return False
        self._logins[day] = self._logins.get(day, 0) + 1
        try:
            api = self._sdk.Shioaji(simulation=self.simulation)
            api.login(api_key=self._key, secret_key=self._secret, subscribe_trade=False)   # 只要行情：不訂委託回報
            api.fetch_contracts(contract_download=True)
            api.set_on_tick_fop_v1_callback(self._callback)
            api.set_on_tick_stk_v1_callback(self._callback)
            api.set_event_callback(self._on_event)
            api.set_session_down_callback(self._on_session_down)
        except Exception as e:
            logger.error("shioaji source: login failed (simulation=%s): %s: %s", self.simulation, type(e).__name__,
                         _scrub(str(e), [self._key, self._secret]))
            return False
        self._api = api
        self._subs.clear()
        self._plan_date = None
        self._replan()
        logger.info("shioaji source connected (simulation=%s): %d subscriptions %s", self.simulation, len(self._subs),
                    sorted(self._subs))
        return True

    def _logout(self) -> None:
        if self._api is None:
            return
        try:
            self._api.logout()
        except Exception as e:
            logger.warning("shioaji source: logout failed: %s", e)
        self._api = None

    # ── 要訂哪幾口 ────────────────────────────────────────────────
    def _trading_date(self, now: str) -> str:
        """現在屬於哪個交易日：盤中＝時段歸屬；盤後（日盤收了、夜盤還沒開）＝下一個交易日；非交易日＝下一個交易日。"""
        for iid in self.instruments:
            inst = self._master.get(iid)
            if inst and inst.get("sessions"):
                session, sd = attribute(inst["sessions"], now)
                if sd:
                    return sd
        d = now[:10]
        if is_trading_day(d) and now[11:16] < "13:45":
            return d
        return get_next_trading_day(d)

    def _replan(self) -> None:
        """依今天的交易日重算訂閱：近月＋下一口（連續代號）、月份合約、現股；退掉不用的、補訂新的。"""
        if self._api is None:
            return
        day = self._trading_date(self._clock())
        if day == self._plan_date:
            return
        self._plan_date = day
        want: dict[str, _Sub] = {}
        hol = self._holidays()
        masters = list(self._master.values())
        for iid in self.instruments:
            inst = self._master.get(iid)
            if inst is None:
                logger.error("shioaji source: %s is not in the instruments master; not subscribed", iid)
                continue
            if inst.get("asset_class") == "future":
                if is_continuous(inst):
                    try:
                        front = front_contract(inst, masters, day, hol)
                    except ValueError as e:
                        logger.error("shioaji source: %s: %s; not subscribed", iid, e)
                        continue
                    if front is None:
                        logger.error("shioaji source: %s has no dated contract for %s in the instruments master (add the "
                                     "current and next month); not subscribed", iid, day)
                        continue
                    dated = dated_contracts(inst, masters)
                    picks = dated[dated.index(front):dated.index(front) + 2]
                    if len(picks) < 2:
                        logger.error("shioaji source: %s: no month after %s in the instruments master; the roll to the next "
                                     "month will have no ticks (add it)", iid, front["instrument_id"])
                    for c in picks:
                        sub = self._future_sub(c, want)
                        if sub:
                            sub.continuous.add(iid)
                elif inst.get("expiry") and inst["expiry"] >= day:
                    sub = self._future_sub(inst, want)
                    if sub:
                        sub.direct.add(iid)
                else:
                    logger.error("shioaji source: %s is neither a continuous future with roll_rule nor a live dated contract; "
                                 "not subscribed", iid)
            elif inst.get("asset_class") in ("equity", "etf"):
                sub = self._stock_sub(inst, want)
                if sub:
                    sub.direct.add(iid)
            else:
                logger.error("shioaji source: %s (%s) is not a future or stock; not subscribed", iid, inst.get("asset_class"))
        if len(want) > settings.SHIOAJI_MAX_SUBSCRIPTIONS:
            logger.error("shioaji source: %d subscriptions > limit %d; keeping the first %d", len(want),
                         settings.SHIOAJI_MAX_SUBSCRIPTIONS, settings.SHIOAJI_MAX_SUBSCRIPTIONS)
            want = dict(list(want.items())[:settings.SHIOAJI_MAX_SUBSCRIPTIONS])
        for code in sorted(set(self._subs) - set(want)):
            self._unsubscribe(self._subs.pop(code))
        for code, sub in want.items():
            if code in self._subs:
                self._subs[code].direct, self._subs[code].continuous = sub.direct, sub.continuous
            elif self._subscribe(sub):
                self._subs[code] = sub
        logger.info("shioaji source plan for %s: %s", day,
                    {c: sorted(s.direct | s.continuous) for c, s in sorted(self._subs.items())})

    def _future_sub(self, dated: dict, want: dict[str, _Sub]) -> _Sub | None:
        m = _MONTH.match(dated.get("symbol") or "")
        if not m:
            logger.error("shioaji source: %s symbol %r is not ROOT+YYYYMM; not subscribed", dated["instrument_id"], dated.get("symbol"))
            return None
        root, month = m.group("root"), m.group("month")
        try:
            group = self._api.Contracts.Futures.get(root)
            found = [c for c in (group or []) if _v(getattr(c, "delivery_month", "")) == month
                     and not _CONTINUOUS_CODE.search(str(getattr(c, "code", "")))]
        except Exception as e:
            logger.error("shioaji source: futures lookup %s failed: %s: %s", root, type(e).__name__, e)
            return None
        if len(found) != 1:
            logger.error("shioaji source: %d contracts for %s %s — expected exactly one; %s not subscribed",
                         len(found), root, month, dated["instrument_id"])
            return None
        c = found[0]
        mult = getattr(c, "multiplier", None) or getattr(c, "unit", None)
        if dated.get("multiplier") and mult and float(mult) != float(dated["multiplier"]):
            logger.error("shioaji source: %s multiplier %s != master %s; not subscribed", dated["instrument_id"], mult,
                         dated["multiplier"])
            return None
        code = str(getattr(c, "code", ""))
        return want.setdefault(code, _Sub(code, c, "future", dated["instrument_id"]))

    def _stock_sub(self, inst: dict, want: dict[str, _Sub]) -> _Sub | None:
        exch = _STOCK_EXCHANGE.get(inst.get("market"))
        code = inst.get("symbol") or ""
        if exch is None or not code.isalnum():
            logger.error("shioaji source: %s is not a TWSE/TPEX stock; not subscribed", inst["instrument_id"])
            return None
        try:
            c = self._api.Contracts.Stocks.get(code)
        except Exception as e:
            logger.error("shioaji source: stock lookup %s failed: %s: %s", code, type(e).__name__, e)
            return None
        if c is None or str(getattr(c, "code", "")) != code or _v(getattr(c, "exchange", "")) != exch:
            logger.error("shioaji source: stock %s resolved to %s on %s (want %s); not subscribed", code,
                         getattr(c, "code", None), _v(getattr(c, "exchange", "")), exch)
            return None
        return want.setdefault(code, _Sub(code, c, "stock", inst["instrument_id"]))

    def _subscribe(self, sub: _Sub) -> bool:
        try:
            self._api.subscribe(sub.contract, quote_type=self._sdk.QuoteType.Tick, intraday_odd=False,
                                version=self._sdk.QuoteVersion.v1)
            return True
        except Exception as e:
            logger.error("shioaji source: subscribe %s failed: %s: %s", sub.code, type(e).__name__, e)
            return False

    def _unsubscribe(self, sub: _Sub) -> None:
        if self._api is None:
            return
        try:
            self._api.unsubscribe(sub.contract, quote_type=self._sdk.QuoteType.Tick, intraday_odd=False,
                                  version=self._sdk.QuoteVersion.v1)
        except Exception as e:
            logger.warning("shioaji source: unsubscribe %s failed: %s", sub.code, e)

    # ── SDK 執行緒（只丟佇列）──────────────────────────────────────
    def _callback(self, *args) -> None:
        if not args:
            return
        try:
            self._q.put_nowait(args[-1])
        except queue.Full:
            self.overflow += 1
            if self.overflow == 1 or self.overflow % 1000 == 0:
                logger.error("shioaji source: tick queue full (%d dropped so far)", self.overflow)

    def _on_event(self, resp_code=None, event_code=None, info=None, event=None, *rest) -> None:
        logger.info("shioaji event resp=%s code=%s info=%s event=%s", resp_code, event_code, info, event)

    def _on_session_down(self, *args) -> None:
        logger.error("shioaji source: session down; reconnecting")
        self._down.set()

    # ── worker ────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.pump(timeout=1.0)
                self._housekeeping()
            except Exception as e:                                    # worker 不能死
                logger.error("shioaji source worker: %s: %s", type(e).__name__, e)

    def pump(self, timeout: float = 0.0, max_items: int | None = None) -> int:
        """把佇列裡的 tick 轉換、路由、交給 runner；回傳交出去的筆數。timeout>0：等第一筆最多這麼久。"""
        n = 0
        block = timeout > 0
        while max_items is None or n < max_items:
            with self._pump_lock:
                try:
                    tick = self._q.get(timeout=timeout) if block else self._q.get_nowait()
                except queue.Empty:
                    break
                self.received += 1
                n += self._route(tick)
            block = False
        return n

    def _housekeeping(self) -> None:
        if self._down.is_set() and time.monotonic() >= self._next_retry:
            self.reconnects += 1
            self._logout()
            if self._connect():
                self._down.clear()
                self._backoff = settings.SHIOAJI_RECONNECT_BACKOFF[0]
            else:
                self._next_retry = time.monotonic() + self._backoff
                logger.error("shioaji source: reconnect failed; next try in %ds", self._backoff)
                self._backoff = min(self._backoff * 2, settings.SHIOAJI_RECONNECT_BACKOFF[1])
            return
        self._replan()
        self._check_silence()

    def _check_silence(self) -> None:
        now = self._clock()
        open_now = any(attribute(self._master[i].get("sessions") or {}, now)[0] for i in self.instruments if i in self._master)
        if not open_now:
            self._last_tick_wall = None
            self._silence_warned = False
            return
        mono = time.monotonic()
        if self._last_tick_wall is None:
            self._last_tick_wall = mono
            return
        if not self._silence_warned and mono - self._last_tick_wall > settings.SHIOAJI_SILENCE_WARN_SECONDS:
            self._silence_warned = True
            logger.warning("shioaji source: no tick for %.0fs during the session (subscriptions %s)", mono - self._last_tick_wall,
                           sorted(self._subs))

    # ── 轉換與路由 ────────────────────────────────────────────────
    def _route(self, tick) -> int:
        code = str(getattr(tick, "code", ""))
        sub = self._subs.get(code)
        if sub is None:
            self.skipped += 1
            return 0
        if getattr(tick, "simtrade", False) or getattr(tick, "suspend", False) or getattr(tick, "intraday_odd", False):
            self.skipped += 1                                         # 試撮、暫停交易、盤中零股：不是整股／期貨的成交
            return 0
        dt = getattr(tick, "datetime", None)
        ts = normalize_ts(dt.isoformat()) if dt is not None else None
        try:
            price = float(str(getattr(tick, "close")))
            vol = int(getattr(tick, "volume"))
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning("shioaji tick %s unreadable (%s); dropped", code, e)
            self.skipped += 1
            return 0
        if ts is None:
            logger.warning("shioaji tick %s has no usable datetime; dropped", code)
            self.skipped += 1
            return 0
        if sub.kind == "stock":
            vol *= SHARES_PER_LOT                                     # 整股：張 → 股（契約的現股量是股）
        self._last_tick_wall = time.monotonic()
        self._silence_warned = False
        targets = set(sub.direct)
        for cid in sub.continuous:
            cont = self._master.get(cid)
            _, day = attribute((cont or {}).get("sessions") or {}, ts)
            if day is None:
                continue                                              # 時段外：runner 也會丟
            try:
                front = front_contract(cont, list(self._master.values()), day, self._holidays())
            except ValueError:
                front = None
            if front is not None and front["instrument_id"] == sub.master_id:
                targets.add(cid)
        n = 0
        for iid in sorted(targets):
            try:
                self._on_tick(iid, ts, price, vol)
                n += 1
            except Exception as e:                                    # runner 的錯不能讓 worker 死
                logger.error("shioaji source: on_tick %s failed: %s", iid, e)
        self.forwarded += n
        if not targets:
            self.skipped += 1
        return n
