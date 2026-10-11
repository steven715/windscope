import logging
from typing import Callable

from config import settings
from db.watchlist import watchlist_list

logger = logging.getLogger(__name__)


def load_watchlist(db_path: str | None) -> list[dict]:
    """觀察名單（DB 表為準，表空即空）；讀取失敗記 log 回空名單，不讓 collector 掛掉。"""
    try:
        return [{"stock_id": r["stock_id"], "stock_name": r["stock_name"]}
                for r in watchlist_list(db_path=db_path)]
    except Exception as e:
        logger.error("watchlist 讀取失敗，視為空名單: %s", e)
        return []


class BaseCollector:
    """所有 collector 的基底：db_path 注入與子任務 collect＋save。"""

    def __init__(self, db_path: str | None = None):
        """db_path 可注入，預設從 settings 讀取。"""
        self.db_path = db_path or settings.DB_PATH

    def _try_collect_and_save(
        self,
        collect_fn: Callable[[], dict | list | None],
        save_fn: Callable[[dict | list], None],
    ) -> bool:
        """執行單一 collect + save 子任務，回傳成功與否。"""
        try:
            data = collect_fn()
            if data is None:
                return False
            save_fn(data)
            return True
        except Exception as e:
            logger.error("%s sub-task failed: %s", self.__class__.__name__, e)
            return False
