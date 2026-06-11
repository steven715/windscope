"""FastAPI app 工廠：組裝 routes、模板、排程器。"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from config import settings

logger = logging.getLogger(__name__)


def create_app(db_path: str | None = None,
               enable_scheduler: bool = False) -> FastAPI:
    """建立 FastAPI app。db_path 為 None 時用 settings.DB_PATH。

    enable_scheduler=True 時啟動 APScheduler（測試時保持 False）。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 確保 schema 存在（冪等）：舊 DB 升級、Docker 首次啟動都靠這行
        from db.connection import get_connection
        from db.schema import create_all_tables

        with get_connection(app.state.db_path) as conn:
            create_all_tables(conn)

        if enable_scheduler:
            from server.scheduler import create_scheduler

            app.state.scheduler = create_scheduler(app.state.db_path)
            app.state.scheduler.start()
            logger.info("Scheduler started")
        yield
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    app = FastAPI(title="台股開盤前情報系統", lifespan=lifespan)
    app.state.db_path = db_path or settings.DB_PATH
    app.state.scheduler = None

    from server.routes import api, pages

    app.include_router(pages.router)
    app.include_router(api.router, prefix="/api")

    return app
