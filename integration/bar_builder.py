"""tick → 已收完的 1 分 K（契約 bars 列）。只在分鐘結束時發出（published_at ＝ 結束時刻），絕不推未收完的 K 棒。

用法：`b = BarBuilder(instrument_id, sessions)`；每筆 tick 呼叫 `on_tick(tick_row, now)` 得到「因為這筆 tick 跨到新分鐘而收完」的 K；
沒有 tick 的分鐘不產 K（停牌不補 Bar）；時鐘走到下一分鐘但沒 tick 時呼叫 `flush(now)` 把上一分鐘收掉。
時段切換（regular→night、跨日）＝不同的 K，不混。
"""

import logging

from integration.market_sessions import attribute

logger = logging.getLogger(__name__)

SOURCE = "windscope.bar_builder"


def _minute_start(ts: str) -> str:
    return ts[:16] + ":00" + ts[19:]


def _minute_end(ts: str) -> str:
    h, m = int(ts[11:13]), int(ts[14:16]) + 1
    if m == 60:
        h, m = h + 1, 0
    if h == 24:                                                    # 23:59 → 次日 00:00
        from datetime import datetime, timedelta
        d = (datetime.fromisoformat(ts[:10]) + timedelta(days=1)).date().isoformat()
        return f"{d}T00:00:00{ts[19:]}"
    return f"{ts[:11]}{h:02d}:{m:02d}:00{ts[19:]}"


def tick_record(instrument_id: str, ts: str, price: float, volume: float, sessions: dict,
                ingested_at: str, source: str) -> dict | None:
    """把一筆成交組成契約 tick 列（resolution=tick、只有 close）；時段外回 None。"""
    session, session_date = attribute(sessions, ts)
    if session is None:
        logger.warning("tick %s@%s outside any session; dropped", instrument_id, ts)
        return None
    return {"instrument_id": instrument_id, "resolution": "tick", "session": session, "session_date": session_date,
            "ts": ts, "open": None, "high": None, "low": None, "close": float(price), "volume": int(volume),
            "turnover": None, "open_interest": None, "source": source, "published_at": ts, "ingested_at": ingested_at,
            "revision": 0}


class BarBuilder:
    """單一商品的 1 分 K 合成器。"""

    def __init__(self, instrument_id: str, sessions: dict) -> None:
        self.instrument_id = instrument_id
        self.sessions = sessions
        self._cur: dict | None = None            # 正在累積的那分鐘

    def on_tick(self, tick: dict, now: str) -> list[dict]:
        """餵一筆契約 tick 列；回傳因此收完的 K（0 或 1 根）。亂序（早於目前分鐘）的 tick 丟掉並記 WARNING。"""
        start = _minute_start(tick["ts"])
        out: list[dict] = []
        if self._cur is not None and start < self._cur["ts"]:
            logger.warning("tick %s@%s older than current minute %s; dropped", self.instrument_id, tick["ts"], self._cur["ts"])
            return out
        if self._cur is not None and (start != self._cur["ts"] or tick["session"] != self._cur["session"]):
            out.append(self._close(now))
        if self._cur is None:
            self._cur = {"instrument_id": self.instrument_id, "resolution": "1m", "session": tick["session"],
                         "session_date": tick["session_date"], "ts": start, "open": tick["close"], "high": tick["close"],
                         "low": tick["close"], "close": tick["close"], "volume": 0, "turnover": None, "open_interest": None,
                         "source": SOURCE, "published_at": _minute_end(start), "ingested_at": now, "revision": 0}
        c = self._cur
        c["high"], c["low"], c["close"] = max(c["high"], tick["close"]), min(c["low"], tick["close"]), tick["close"]
        c["volume"] += int(tick.get("volume") or 0)
        return out

    def flush(self, now: str) -> list[dict]:
        """時鐘到了 now：若正在累積的那分鐘已經結束（now ≥ 結束時刻），收掉它。"""
        if self._cur is not None and now >= self._cur["published_at"]:
            return [self._close(now)]
        return []

    def _close(self, now: str) -> dict:
        bar, self._cur = self._cur, None
        bar["ingested_at"] = max(now, bar["published_at"])
        return bar
