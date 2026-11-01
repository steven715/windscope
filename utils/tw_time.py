"""Asia/Taipei 時刻工具：契約要求所有時刻為 `YYYY-MM-DDTHH:MM:SS+08:00`，與容器 TZ 無關。"""

from datetime import datetime
from zoneinfo import ZoneInfo

TW = ZoneInfo("Asia/Taipei")


def now_tw() -> datetime:
    """現在的 Asia/Taipei 時刻（tz-aware）。"""
    return datetime.now(TW)


def now_tw_iso() -> str:
    """現在時刻的契約格式字串（秒精度、+08:00）。"""
    return now_tw().replace(microsecond=0).isoformat()


def tw_iso(date: str, hhmm: str) -> str:
    """把 `YYYY-MM-DD` ＋ `HH:MM` 組成契約格式時刻。"""
    return f"{date}T{hhmm}:00+08:00"
