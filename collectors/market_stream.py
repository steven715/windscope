"""行情源（Layer 1，即時）：把來源的成交轉成契約 tick、合成 1 分 K、落地 bars 表、推進 StreamHub。

來源介面 `MarketSource.start(on_tick, on_bar) -> bool`／`stop()`：
  on_tick(instrument_id, ts, price, volume)——逐筆成交；on_bar(bar_row)——來源本身就是已收完的 1m（只有回放用）。
- `ReplaySource`：回放契約 bars 列——tick 列走 on_tick（經合成器），1m 列走 on_bar（直接推、直接落地，**不**拆成假 tick：
  消費端的 `last` 不會看到市場沒走過的路徑）；同一商品同一分鐘兩者都有只用 tick。
- `ShioajiSource`：永豐 API（📌 STUB）。雲端連不到券商、沒有憑證；本機驗證前 `start()` 回 False。
Runner：來源的 ts 先經 db/bars.normalize_ts 正規化（壞時刻丟掉並記 WARNING）、每筆都過契約驗證才推；時段歸屬用主檔 sessions；
時鐘驅動的 flush（clock_flush=True 時每秒看一次）讓沒有後續 tick 的分鐘也在結束時刻發出。
tick 落地（STREAM_PERSIST_TICKS）是有損的：契約 ts 只到秒，同一秒多筆 tick 會互相覆蓋；預設關。
"""

import logging
import threading
import time
from typing import Callable, Protocol

from config import settings
from db.bars import bars_upsert, normalize_ts
from db.connection import get_connection
from db.instruments import instrument_get
from integration.bar_builder import BarBuilder, tick_record
from utils.contract_schema import validate_record
from utils.tw_time import now_tw_iso

logger = logging.getLogger(__name__)

OnTick = Callable[[str, str, float, float | None], None]
OnBar = Callable[[dict], None]


class MarketSource(Protocol):
    """行情來源。"""

    name: str

    def start(self, on_tick: OnTick, on_bar: OnBar | None = None) -> bool: ...

    def stop(self) -> None: ...


class ReplaySource:
    """回放契約 bars 列（tick → on_tick、1m → on_bar）。delay_sec>0 在背景執行緒跑，`wait()` 等它跑完。"""

    name = "replay"

    def __init__(self, records: list[dict], delay_sec: float = 0.0) -> None:
        tick_minutes = {(r["instrument_id"], r["ts"][:16]) for r in records if r["resolution"] == "tick"}
        keep = [r for r in records if r["resolution"] == "tick"
                or (r["resolution"] == "1m" and r.get("open") is not None
                    and (r["instrument_id"], r["ts"][:16]) not in tick_minutes)]
        self._records = sorted(keep, key=lambda r: (r["ts"], r["resolution"] != "tick"))
        self._delay = delay_sec
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.replayed = 0

    def __len__(self) -> int:
        return len(self._records)

    def _run(self, on_tick: OnTick, on_bar: OnBar | None) -> None:
        for r in self._records:
            if self._stop.is_set():
                return
            if r["resolution"] == "tick":
                on_tick(r["instrument_id"], r["ts"], r["close"], r.get("volume"))
            elif on_bar is not None:
                on_bar(dict(r))
            self.replayed += 1
            if self._delay:
                time.sleep(self._delay)

    def start(self, on_tick: OnTick, on_bar: OnBar | None = None) -> bool:
        """開始回放；delay_sec=0 同步跑完才回傳，否則背景執行緒跑（用 wait() 等）。"""
        if self._delay:
            self._thread = threading.Thread(target=self._run, args=(on_tick, on_bar), daemon=True, name="replay-source")
            self._thread.start()
        else:
            self._run(on_tick, on_bar)
        return True

    def wait(self, timeout: float | None = None) -> None:
        """等背景回放跑完（不中斷它）。"""
        if self._thread:
            self._thread.join(timeout)

    def stop(self) -> None:
        """中斷回放。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class ShioajiSource:
    """永豐 Shioaji 行情（📌 STUB，本機驗證後才實作；驗證清單在 docs/data_sources.md「永豐 Shioaji」）。"""

    name = "shioaji"

    def start(self, on_tick: OnTick, on_bar: OnBar | None = None) -> bool:
        # TODO 狀態:STUB
        # 要做的事（依 Shioaji 文件，未實測，不猜）：
        #   1. 以 SHIOAJI_API_KEY／SHIOAJI_SECRET 登入（只要行情，不簽署下單憑證；與 tradewind 的下單登入是否能分開 ⚠ 待查證）。
        #   2. 依 settings.STREAM_INSTRUMENTS 找合約：TAIFEX:TMF → 微台近月（api.Contracts.Futures.TMF，近月對應規則＝instruments.roll_rule）。
        #   3. 訂閱逐筆（quote_type Tick），callback 轉 (instrument_id, ts, price, volume) 丟給 on_tick；ts 轉成 +08:00 契約格式
        #      （runner 會再 normalize_ts 一次，壞時刻會被丟）。
        #   4. 斷線重連、登入逾時、每日連線數限制：記 log、不丟例外。
        """尚未驗證：記 ERROR、回 False，不啟動。"""
        logger.error("ShioajiSource is a stub: verify on a machine with credentials and network (docs/data_sources.md)")
        return False

    def stop(self) -> None:
        """無事可停。"""


class StreamRunner:
    """接來源 → tick 列 → publish；合成 1m → publish ＋ 落地 bars 表；clock_flush 時每秒把已結束的分鐘收掉。"""

    def __init__(self, source: MarketSource, hub, db_path: str | None = None,
                 instruments: list[str] | None = None, persist_ticks: bool | None = None,
                 clock_flush: bool = True) -> None:
        self.source = source
        self.hub = hub
        self.db_path = db_path
        self.instruments = list(instruments or settings.STREAM_INSTRUMENTS)
        self.persist_ticks = settings.STREAM_PERSIST_TICKS if persist_ticks is None else persist_ticks
        self.clock_flush = clock_flush
        self._builders: dict[str, BarBuilder] = {}
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._flusher: threading.Thread | None = None
        self._stop = threading.Event()
        self.ticks = 0
        self.bars = 0
        self.dropped = 0

    def _sessions_for(self, instrument_id: str) -> dict | None:
        if not self._sessions.get(instrument_id):                     # 空的不快取：主檔補上後下一筆就能用
            inst = instrument_get(instrument_id, db_path=self.db_path)
            self._sessions[instrument_id] = (inst or {}).get("sessions") or {}
        return self._sessions[instrument_id] or None

    def on_tick(self, instrument_id: str, ts: str, price: float, volume: float | None) -> None:
        """來源 callback（可能來自別的執行緒）：正規化時刻、組 tick、驗契約、推、合成。"""
        if instrument_id not in self.instruments:
            return
        sessions = self._sessions_for(instrument_id)
        if not sessions:
            logger.warning("no sessions for %s in instruments; tick dropped", instrument_id)
            self.dropped += 1
            return
        norm = normalize_ts(ts)
        if norm is None:
            logger.warning("tick %s has unparseable ts %r; dropped", instrument_id, ts)
            self.dropped += 1
            return
        now = now_tw_iso()
        with self._lock:
            tick = tick_record(instrument_id, norm, price, volume, sessions, max(now, norm), f"broker.{self.source.name}")
            if tick is None:
                self.dropped += 1
                return
            errs = validate_record("bars", tick)
            if errs:
                logger.warning("tick %s@%s violates contract: %s; dropped", instrument_id, norm, errs[:2])
                self.dropped += 1
                return
            self.ticks += 1
            self.hub.publish(tick)
            if self.persist_ticks:
                self._persist([tick])
            b = self._builders.setdefault(instrument_id, BarBuilder(instrument_id, sessions))
            for bar in b.on_tick(tick, max(now, norm)):
                self._emit_bar(bar)

    def on_bar(self, bar: dict) -> None:
        """來源直接給已收完的 1m（回放）：驗契約、推、落地；不經合成器。"""
        if bar.get("instrument_id") not in self.instruments or bar.get("resolution") != "1m":
            return
        errs = validate_record("bars", bar)
        if errs:
            logger.warning("replayed bar %s@%s violates contract: %s; dropped", bar.get("instrument_id"), bar.get("ts"), errs[:2])
            self.dropped += 1
            return
        with self._lock:
            self._emit_bar(bar)

    def flush(self, now: str | None = None) -> int:
        """時鐘驅動：把已結束但沒新 tick 的分鐘收掉；回傳收掉的 K 數。"""
        n = 0
        with self._lock:
            for b in self._builders.values():
                for bar in b.flush(now or now_tw_iso()):
                    self._emit_bar(bar)
                    n += 1
        return n

    def _flush_loop(self) -> None:
        while not self._stop.wait(settings.STREAM_FLUSH_SECONDS):
            try:
                self.flush()
            except Exception as e:                                    # 不能讓時鐘執行緒死掉
                logger.error("stream flush failed: %s", e)

    def _emit_bar(self, bar: dict) -> None:
        self.bars += 1
        self.hub.publish(bar)
        self._persist([bar])

    def _persist(self, records: list[dict]) -> None:
        try:
            with get_connection(self.db_path) as conn:
                bars_upsert(conn, records)
        except Exception as e:                                        # 落地失敗不能讓串流掛掉
            logger.error("bars persist failed: %s", e)

    def start(self) -> bool:
        """啟動來源（來源丟例外也只記 ERROR）；clock_flush 時啟動每秒 flush 的執行緒。"""
        logger.info("market stream start: source=%s instruments=%s", self.source.name, self.instruments)
        try:
            ok = self.source.start(self.on_tick, self.on_bar)
        except Exception as e:
            logger.error("market stream source %s raised on start: %s", self.source.name, e)
            return False
        if not ok:
            logger.error("market stream source %s failed to start", self.source.name)
            return False
        if self.clock_flush:
            self._flusher = threading.Thread(target=self._flush_loop, daemon=True, name="stream-flush")
            self._flusher.start()
        return True

    def stop(self, now: str | None = None) -> None:
        """停來源、停時鐘、收掉手上的 K（now 給了就以它當時鐘：回放歷史時用最後一筆之後）。"""
        self._stop.set()
        if self._flusher:
            self._flusher.join(timeout=5)
        try:
            self.source.stop()
        except Exception as e:
            logger.error("market stream source %s raised on stop: %s", self.source.name, e)
        self.flush(now)
        logger.info("market stream stop: %d ticks, %d bars, %d dropped", self.ticks, self.bars, self.dropped)


def make_source(name: str, replay_records: list[dict] | None = None, delay_sec: float = 0.0) -> MarketSource | None:
    """依名稱建來源：none → None、replay、shioaji；未知名稱 raise ValueError（呼叫端決定要不要擋）。"""
    if name == "none":
        return None
    if name == "replay":
        return ReplaySource(replay_records or [], delay_sec)
    if name == "shioaji":
        return ShioajiSource()
    raise ValueError(f"unknown MARKET_SOURCE {name!r}")


def start_market_stream(hub, db_path: str | None) -> StreamRunner | None:
    """server 啟動時依 settings 接來源；任何錯誤只記 ERROR、回 None，server 照常。"""
    try:
        records = None
        if settings.MARKET_SOURCE == "replay":
            from db.bars import bars_from_jsonl
            if not settings.STREAM_REPLAY_JSONL:
                logger.error("MARKET_SOURCE=replay needs STREAM_REPLAY_JSONL; stream not started")
                return None
            records = bars_from_jsonl(settings.STREAM_REPLAY_JSONL)
        source = make_source(settings.MARKET_SOURCE, records, settings.STREAM_REPLAY_DELAY_SECONDS)
    except Exception as e:
        logger.error("market stream not started: %s", e)
        return None
    if source is None:
        return None
    runner = StreamRunner(source, hub, db_path)
    return runner if runner.start() else None
