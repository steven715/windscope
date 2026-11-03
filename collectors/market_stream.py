"""行情源（Layer 1，即時）：把來源的成交轉成契約 tick、合成 1 分 K、落地 bars 表、推進 StreamHub。

來源介面 `MarketSource.start(on_tick) -> bool`／`stop()`：on_tick(instrument_id, ts, price, volume)。
- `ReplaySource`：把契約 bars 列（tick 或 1m）依序回放——測試、離線整合（tradewind 在雲端對 `/ws/bars` 跑）、本機演練用。
- `ShioajiSource`：永豐 API（📌 STUB）。雲端連不到券商、沒有憑證，這裡只定義它要做的事；本機驗證前 `start()` 回 False。
Runner 不打網路、不猜規則：時段歸屬用主檔 sessions（integration/market_sessions），1 分 K 用 integration/bar_builder。
"""

import logging
import threading
from typing import Callable, Protocol

from config import settings
from db.bars import bars_upsert
from db.connection import get_connection
from db.instruments import instrument_get
from integration.bar_builder import BarBuilder, tick_record
from utils.tw_time import now_tw_iso

logger = logging.getLogger(__name__)

OnTick = Callable[[str, str, float, float], None]


class MarketSource(Protocol):
    """行情來源。"""

    name: str

    def start(self, on_tick: OnTick) -> bool: ...

    def stop(self) -> None: ...


class ReplaySource:
    """回放契約 bars 列：tick 列→on_tick；1m 列→拆成開／高／低／收四筆 tick，讓合成結果等於原 K。
    同一商品同一分鐘既有 tick 又有 1m 時只用 tick（真實成交），1m 列略過，避免重複計量。"""

    name = "replay"

    def __init__(self, records: list[dict], delay_sec: float = 0.0) -> None:
        tick_minutes = {(r["instrument_id"], r["ts"][:16]) for r in records if r["resolution"] == "tick"}
        keep = [r for r in records if r["resolution"] == "tick"
                or (r["resolution"] == "1m" and (r["instrument_id"], r["ts"][:16]) not in tick_minutes)]
        self._records = sorted(keep, key=lambda r: (r["ts"], r["resolution"] != "tick"))
        self._delay = delay_sec
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def _run(self, on_tick: OnTick) -> None:
        import time
        for r in self._records:
            if self._stop.is_set():
                return
            if r["resolution"] == "tick":
                on_tick(r["instrument_id"], r["ts"], r["close"], r.get("volume") or 0)
            elif r["resolution"] == "1m" and r.get("open") is not None:
                ts = r["ts"]
                on_tick(r["instrument_id"], ts, r["open"], 0)
                on_tick(r["instrument_id"], ts, r["high"], 0)
                on_tick(r["instrument_id"], ts, r["low"], 0)
                on_tick(r["instrument_id"], ts[:17] + "59" + ts[19:], r["close"], r.get("volume") or 0)
            if self._delay:
                time.sleep(self._delay)

    def start(self, on_tick: OnTick) -> bool:
        """開始回放；delay_sec=0 時同步跑完才回傳，否則在背景執行緒跑。"""
        if self._delay:
            self._thread = threading.Thread(target=self._run, args=(on_tick,), daemon=True, name="replay-source")
            self._thread.start()
        else:
            self._run(on_tick)
        return True

    def stop(self) -> None:
        """停止回放。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


class ShioajiSource:
    """永豐 Shioaji 行情（📌 STUB，本機驗證後才實作）。

    # TODO 狀態:STUB
    # 要做的事（依 Shioaji 文件，未實測，不猜）：
    #   1. 以 SHIOAJI_API_KEY／SHIOAJI_SECRET 登入（只要行情，不簽署下單憑證；與 tradewind 的下單登入是否能分開 ⚠ 待查證）。
    #   2. 依 settings.STREAM_INSTRUMENTS 找合約：TAIFEX:TMF → 微台近月（api.Contracts.Futures.TMF，近月對應規則＝instruments.roll_rule）。
    #   3. 訂閱逐筆（quote_type Tick），callback 轉 (instrument_id, ts, price, volume) 丟給 on_tick；ts 用交易所時間轉 +08:00。
    #   4. 斷線重連、登入逾時、每日連線數限制：記 log、不丟例外。
    # 驗證清單在 docs/data_sources.md「永豐 Shioaji」。
    """

    name = "shioaji"

    def start(self, on_tick: OnTick) -> bool:
        """尚未驗證：記 ERROR、回 False，不啟動。"""
        logger.error("ShioajiSource is a stub: verify on a machine with credentials and network (docs/data_sources.md)")
        return False

    def stop(self) -> None:
        """無事可停。"""


class StreamRunner:
    """接來源 → tick 列 → publish；合成 1m → publish ＋ 落地 bars 表。"""

    def __init__(self, source: MarketSource, hub, db_path: str | None = None,
                 instruments: list[str] | None = None, persist_ticks: bool | None = None) -> None:
        self.source = source
        self.hub = hub
        self.db_path = db_path
        self.instruments = list(instruments or settings.STREAM_INSTRUMENTS)
        self.persist_ticks = settings.STREAM_PERSIST_TICKS if persist_ticks is None else persist_ticks
        self._builders: dict[str, BarBuilder] = {}
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.ticks = 0
        self.bars = 0

    def _sessions_for(self, instrument_id: str) -> dict | None:
        if instrument_id not in self._sessions:
            inst = instrument_get(instrument_id, db_path=self.db_path)
            self._sessions[instrument_id] = (inst or {}).get("sessions") or {}
        return self._sessions[instrument_id] or None

    def on_tick(self, instrument_id: str, ts: str, price: float, volume: float) -> None:
        """來源 callback（可能來自別的執行緒）。"""
        if instrument_id not in self.instruments:
            return
        sessions = self._sessions_for(instrument_id)
        if not sessions:
            logger.warning("no sessions for %s in instruments; tick dropped", instrument_id)
            return
        now = now_tw_iso()
        with self._lock:
            tick = tick_record(instrument_id, ts, price, volume, sessions, now, f"broker.{self.source.name}")
            if tick is None:
                return
            self.ticks += 1
            self.hub.publish(tick)
            if self.persist_ticks:
                self._persist([tick])
            b = self._builders.setdefault(instrument_id, BarBuilder(instrument_id, sessions))
            for bar in b.on_tick(tick, max(now, tick["ts"])):
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
        """啟動來源。"""
        logger.info("market stream start: source=%s instruments=%s", self.source.name, self.instruments)
        ok = self.source.start(self.on_tick)
        if not ok:
            logger.error("market stream source %s failed to start", self.source.name)
        return ok

    def stop(self) -> None:
        """停來源、收掉手上的 K。"""
        self.source.stop()
        self.flush()
        logger.info("market stream stop: %d ticks, %d bars", self.ticks, self.bars)


def make_source(name: str, replay_records: list[dict] | None = None, delay_sec: float = 0.0) -> MarketSource | None:
    """依名稱建來源：none → None、replay、shioaji。"""
    if name == "none":
        return None
    if name == "replay":
        return ReplaySource(replay_records or [], delay_sec)
    if name == "shioaji":
        return ShioajiSource()
    raise ValueError(f"unknown MARKET_SOURCE {name!r}")
