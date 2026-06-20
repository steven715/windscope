import logging
import os
import sys


def setup_logging(log_dir: str = "logs") -> None:
    """設定 root logger：INFO 到 console，ERROR 以上也寫入 logs/error.log。"""
    os.makedirs(log_dir, exist_ok=True)

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
