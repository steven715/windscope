"""行情串流的 in-process hub：來源（collectors/market_stream）publish，WebSocket 連線 subscribe；執行緒安全。

每個訂閱者一個 queue；publish 不等任何人（慢的訂閱者滿了就丟並計數，不拖垮來源）。
心跳由 WS 路由在閒置時送（契約：每 N 秒 `{"heartbeat": ts}`），hub 本身不管時間。
"""

import logging
import queue
import threading

logger = logging.getLogger(__name__)

_MAX_QUEUE = 10_000


class StreamHub:
    """發佈／訂閱。"""

    def __init__(self) -> None:
        self._subs: dict[int, tuple[queue.Queue, set[str] | None]] = {}
        self._lock = threading.Lock()
        self._next = 0
        self.published = 0
        self.dropped = 0
        self.last_record: dict | None = None

    def subscribe(self, instruments: set[str] | None = None) -> int:
        """訂閱（可只要某些商品）；回訂閱 id。"""
        with self._lock:
            sid, self._next = self._next, self._next + 1
            self._subs[sid] = (queue.Queue(maxsize=_MAX_QUEUE), instruments)
            return sid

    def unsubscribe(self, sid: int) -> None:
        """取消訂閱。"""
        with self._lock:
            self._subs.pop(sid, None)

    def get(self, sid: int, timeout: float) -> dict | None:
        """訂閱者取下一筆（阻塞至多 timeout 秒）；沒有回 None。"""
        q = self._subs.get(sid)
        if q is None:
            return None
        try:
            return q[0].get(timeout=timeout)
        except queue.Empty:
            return None

    def publish(self, record: dict) -> None:
        """廣播一筆契約 bars 列（tick 或已收完的 1m）。"""
        self.published += 1
        self.last_record = record
        with self._lock:
            subs = list(self._subs.values())
        for q, wanted in subs:
            if wanted and record.get("instrument_id") not in wanted:
                continue
            try:
                q.put_nowait(record)
            except queue.Full:
                self.dropped += 1

    @property
    def subscribers(self) -> int:
        """目前訂閱數。"""
        return len(self._subs)
