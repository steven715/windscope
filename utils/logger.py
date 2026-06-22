import json
import logging
import os
import sys
from datetime import datetime

# 結構化事件專用 logger：log_event 把事件以一行 JSON 寫到 logs/events.jsonl，
# 與人類可讀 log 並存、互不干擾（propagate=False，由 setup_logging 配置 handler）。
# 模組載入即設 INFO，確保即使 setup_logging 尚未呼叫，事件仍會交給已掛的 handler。
_EVENTS_LOGGER_NAME = "windscope.events"
_events_logger = logging.getLogger(_EVENTS_LOGGER_NAME)
_events_logger.setLevel(logging.INFO)


def setup_logging(log_dir: str = "logs") -> None:
    """設定 root logger：INFO 到 console，ERROR 以上也寫入 logs/error.log。"""
    os.makedirs(log_dir, exist_ok=True)

    # 結構化事件：獨立檔、純 JSON 行（可直接 jq）、不向 root 傳遞以免被人類格式污染。
    # 與 root handler 分開配置（各有 guard），放在 root 早退守衛之前。
    if not _events_logger.handlers:
        events_handler = logging.FileHandler(
            os.path.join(log_dir, "events.jsonl"), encoding="utf-8"
        )
        events_handler.setLevel(logging.INFO)
        events_handler.setFormatter(logging.Formatter("%(message)s"))
        _events_logger.addHandler(events_handler)
        _events_logger.propagate = False

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 避免重複 handler（模組被多次 import 時）
    if root_logger.handlers:
        return

    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — INFO 以上
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler — ERROR 以上
    file_handler = logging.FileHandler(
        os.path.join(log_dir, "error.log"), encoding="utf-8"
    )
    file_handler.setLevel(logging.ERROR)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # 盤中即時刷新每 12 秒觸發一次，apscheduler 每次都記一條 INFO「Running job…/executed」，
    # 一天上萬筆灌爆 log。把 executor 的逐次執行記錄壓到 WARNING（失敗仍會出現），
    # job 本身的有意義結果由各 job 自己的 logger 記錄、不受影響。
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    """發一筆結構化事件（一行 JSON）到 windscope.events。additive、永不 raise。

    參數：event 事件名；level log 等級；**fields 任意附加欄位。
    輸出 {"event":..., "ts": <Asia/Taipei ISO>, **fields}（ts 依容器 TZ）。
    """
    try:
        payload = {"event": event, "ts": datetime.now().isoformat(), **fields}
        _events_logger.log(
            level, json.dumps(payload, ensure_ascii=False, default=str)
        )
    except Exception:
        # 觀測層絕不能拖垮主流程：任何序列化/輸出問題都吞掉。
        pass
