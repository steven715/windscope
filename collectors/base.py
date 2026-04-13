import logging
from abc import ABC, abstractmethod
from typing import Callable

from config import settings

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """所有 collector 的抽象基底類別。"""

    def __init__(self, db_path: str | None = None):
        """db_path 可注入，預設從 settings 讀取。"""
        self.db_path = db_path or settings.DB_PATH

    @abstractmethod
    def collect(self, date: str) -> dict | None:
        """收集指定日期的資料。成功回傳 dict，失敗回傳 None。"""

    @abstractmethod
    def save(self, date: str, data: dict) -> None:
        """存入 SQLite raw table。"""

    def run(self, date: str) -> bool:
        """collect -> save。成功回傳 True，失敗回傳 False 並 log error。"""
        logger.info("%s: starting for %s", self.__class__.__name__, date)
        try:
            data = self.collect(date)
            if data is None:
                logger.warning("%s: no data for %s", self.__class__.__name__, date)
                return False
            self.save(date, data)
            logger.info("%s: saved data for %s", self.__class__.__name__, date)
            return True
        except Exception as e:
            logger.error("%s failed for %s: %s", self.__class__.__name__, date, e)
            return False

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
