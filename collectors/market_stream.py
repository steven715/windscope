"""行情源（Layer 1，即時）：把來源的成交轉成契約 tick、合成 1 分 K、落地 bars 表、推進 StreamHub。

來源介面 `MarketSource.start(on_tick, on_bar) -> bool`／`stop()`：
  on_tick(instrument_id, ts, price, volume)——逐筆成交；on_bar(bar_row)——來源本身就是已收完的 1m（只有回放用）。
- `ReplaySource`：回放契約 bars 列——tick 列走 on_tick（經合成器），1m 列走 on_bar（直接推、直接落地，**不**拆成假 tick：
  消費端的 `last` 不會看到市場沒走過的路徑）；同一商品同一分鐘兩者都有只用 tick。
- `ShioajiSource`（collectors/shioaji_source.py）：永豐逐筆成交；連續代號依 strategylib.roll 的近月路由（⚠ 本機驗證清單在 docs/data_sources.md）。
Runner：來源的 ts 先經 db/bars.normalize_ts 正規化（壞時刻丟掉並記 WARNING）、每筆都過契約驗證才推；時段歸屬用主檔 sessions；
時鐘驅動的 flush（clock_flush=True 時每秒看一次）讓沒有後續 tick 的分鐘也在結束時刻發出。
tick 落地（STREAM_PERSIST_TICKS）是有損的：契約 ts 只到秒，同一秒多筆 tick 會互相覆蓋；預設關。
**串流演練隔離**：來源是 ReplaySource 時，落地一律寫 settings.REPLAY_DB_PATH（第一次寫時建表），讀主檔（sessions）仍用 db_path；
演練 DB 解析到正式 DB（設定錯）在建 runner 時就關掉落地、記一次 ERROR（不碰正式 DB 的 schema）；bars_upsert(replay=True) 也會擋。
演練只隔離了落地：/ws/bars 推出去的 1m 跟真的長得一樣，演練要跑在另一個 server（port），不要讓 tradewind 的 paper 排程接它。
"""

import logging
import os
import threading
import time
from typing import Callable, Protocol

from config import settings
from db.bars import bars_upsert, is_protected_path, normalize_ts
from db.connection import get_connection
from db.instruments import instrument_get
from db.schema import create_all_tables
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
    rehearsal = True                                                    # 串流演練：runner 落地到演練 DB

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


class StreamRunner:
    """接來源 → tick 列 → publish；合成 1m → publish ＋ 落地 bars 表；clock_flush 時每秒把已結束的分鐘收掉。"""

    def __init__(self, source: MarketSource, hub, db_path: str | None = None,
                 instruments: list[str] | None = None, persist_ticks: bool | None = None,
                 clock_flush: bool = True, persist_db: str | None = None) -> None:
        self.source = source
        self.hub = hub
        self.db_path = db_path                                          # 讀主檔（sessions）
        self.replay = bool(getattr(source, "rehearsal", False))
        self.persist_db = persist_db or (settings.REPLAY_DB_PATH if self.replay else db_path)   # 演練一律寫演練 DB
        self._schema_ready = not self.replay
        self._persist_off = False
        if self.replay and is_protected_path(self.persist_db or settings.DB_PATH):            # 設定錯：一次 ERROR，整場不落地
            logger.error("rehearsal persist DB %s is the production DB; bars will NOT be persisted (set REPLAY_DB)",
                         self.persist_db or settings.DB_PATH)
            self._persist_off = True
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
        self.persisted = 0                                              # 實際落地的 1m 筆數（bars 是推出去的）
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
        self.persisted += self._persist([bar])

    def _persist(self, records: list[dict]) -> int:
        if self._persist_off:
            return 0
        try:
            if not self._schema_ready and self.persist_db and self.persist_db != ":memory:":
                os.makedirs(os.path.dirname(self.persist_db) or ".", exist_ok=True)   # 演練 DB 的目錄可能不存在
            with get_connection(self.persist_db) as conn:
                if not self._schema_ready:                              # 演練 DB 可能是新檔：第一次寫時建表（冪等）
                    create_all_tables(conn)
                    self._schema_ready = True
                return bars_upsert(conn, records, replay=self.replay)
        except Exception as e:                                        # 落地失敗不能讓串流掛掉
            logger.error("bars persist failed (%s): %s", self.persist_db, e)
            return 0

    def start(self) -> bool:
        """啟動來源（來源丟例外也只記 ERROR）；clock_flush 時啟動每秒 flush 的執行緒。"""
        logger.info("market stream start: source=%s instruments=%s persist=%s", self.source.name, self.instruments, self.persist_db)
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


def make_source(name: str, replay_records: list[dict] | None = None, delay_sec: float = 0.0,
                db_path: str | None = None) -> MarketSource | None:
    """依名稱建來源：none → None、replay、shioaji（讀 db_path 的主檔）；未知名稱 raise ValueError（呼叫端決定要不要擋）。"""
    if name == "none":
        return None
    if name == "replay":
        return ReplaySource(replay_records or [], delay_sec)
    if name == "shioaji":
        from collectors.shioaji_source import ShioajiSource
        return ShioajiSource(settings.STREAM_INSTRUMENTS, db_path=db_path)
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
        source = make_source(settings.MARKET_SOURCE, records, settings.STREAM_REPLAY_DELAY_SECONDS, db_path)
    except Exception as e:
        logger.error("market stream not started: %s", e)
        return None
    if source is None:
        return None
    runner = StreamRunner(source, hub, db_path)
    return runner if runner.start() else None
