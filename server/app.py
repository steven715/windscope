"""FastAPI app 工廠：組裝 routes、模板、排程器。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import settings

logger = logging.getLogger(__name__)


def create_app(db_path: str | None = None,
               enable_scheduler: bool = False, api_token: str | None = None) -> FastAPI:
    """建立 FastAPI app。db_path 為 None 時用 settings.DB_PATH；api_token 為 None 時用 settings.WINDSCOPE_API_TOKEN（空＝不認證）。

    enable_scheduler=True 時啟動 APScheduler（測試時保持 False）。
    """
    token = settings.WINDSCOPE_API_TOKEN if api_token is None else api_token

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 確保 schema 存在（冪等）：舊 DB 升級、Docker 首次啟動都靠這行
        from db.connection import get_connection
        from db.schema import create_all_tables

        with get_connection(app.state.db_path) as conn:
            create_all_tables(conn)
        if not token:
            logger.error("WINDSCOPE_API_TOKEN is not set: every endpoint (including the POSTs that change data) is open; "
                         "never expose this server (ngrok, port forwarding) without it")

        if enable_scheduler:
            from server.scheduler import create_scheduler

            app.state.scheduler = create_scheduler(app.state.db_path)
            app.state.scheduler.start()
            logger.info("Scheduler started")
            # 即時行情：有設 MARKET_SOURCE 才接；設定錯（金鑰、SHIOAJI_SIMULATION、名稱打錯）只記 ERROR，server 照常；永豐的登入在背景做，不擋開機
            from collectors.market_stream import start_market_stream

            app.state.stream = start_market_stream(app.state.hub, app.state.db_path)
        yield
        if app.state.stream is not None:
            app.state.stream.stop()
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    app = FastAPI(title="台股開盤前情報系統", lifespan=lifespan)
    app.state.db_path = db_path or settings.DB_PATH
    app.state.scheduler = None
    app.state.stream = None

    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    from server.routes import api, pages, stream
    from server.stream import StreamHub

    app.state.hub = StreamHub()
    app.include_router(pages.router)
    app.include_router(api.router, prefix="/api")
    app.include_router(stream.router)

    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/healthz")
    def healthz() -> dict:
        """存活檢查（公開，不需 token）：只回是否活著與認證有沒有開。"""
        return {"ok": True, "auth": bool(token)}

    from server.auth import TokenAuth

    app.add_middleware(TokenAuth, token=token)                       # 單一 token（ADR-0008）：HTTP 與 /ws/bars 都過這裡
    return app
