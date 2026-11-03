"""WebSocket `/ws/bars`：即時推逐筆 tick 與已收完的 1 分 K（契約 bars 列）＋心跳（契約 README「串流與回放」）。

- `?instruments=TAIFEX:TMF,TWSE:2330` 只收這些商品；不給＝全部。
- 每筆一個 JSON 物件；閒置超過 settings.STREAM_HEARTBEAT_SECONDS 送 `{"heartbeat": "<now>"}`。
- 只轉發 hub 裡的東西：來源沒開（MARKET_SOURCE=none）時只會收到心跳。
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config import settings
from utils.tw_time import now_tw_iso

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/bars")
async def ws_bars(ws: WebSocket) -> None:
    """串流端點。"""
    hub = ws.app.state.hub
    wanted = ws.query_params.get("instruments")
    sid = hub.subscribe({x.strip() for x in wanted.split(",") if x.strip()} if wanted else None)
    await ws.accept()
    logger.info("ws/bars client connected (sub %s, instruments=%s)", sid, wanted or "all")
    try:
        while True:
            rec = await asyncio.to_thread(hub.get, sid, settings.STREAM_HEARTBEAT_SECONDS)
            await ws.send_json(rec if rec is not None else {"heartbeat": now_tw_iso()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hub.unsubscribe(sid)
        logger.info("ws/bars client disconnected (sub %s)", sid)
