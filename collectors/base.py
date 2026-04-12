import logging
from abc import ABC, abstractmethod

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
        """存入 SQLite raw table。用 INSERT OR REPLACE。"""

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
