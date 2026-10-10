"""Job 共用工具：步驟執行、狀態判定。"""

import logging
import time
from collections.abc import Callable
from typing import TypeVar

from utils.logger import log_event

logger = logging.getLogger(__name__)

T = TypeVar("T")


def run_step_value(step_name: str, fn: Callable[[], T]) -> tuple[T | None, str | None]:
    """執行單一步驟並帶回結果，回傳 (結果, 錯誤訊息)；fn 回 None/False 或拋例外時結果為 None。"""
    t0 = time.perf_counter()
    try:
        result = fn()
        duration_ms = int((time.perf_counter() - t0) * 1000)
        if result is None or result is False:
            log_event("step_run", level=logging.WARNING, step=step_name,
                      outcome="failed", duration_ms=duration_ms,
                      error=f"{step_name}: no data")
            return None, f"{step_name}: no data"
        log_event("step_run", step=step_name, outcome="ok",
                  duration_ms=duration_ms)
        return result, None
    except Exception as e:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        logger.error("%s failed: %s", step_name, e)
        # 例外＝真正的錯誤 → ERROR（與 logger.error、collector_run 例外分支一致）；
        # no-data 降級維持 WARNING。
        log_event("step_run", level=logging.ERROR, step=step_name,
                  outcome="failed", duration_ms=duration_ms,
                  error=f"{step_name}: {str(e)}")
        return None, f"{step_name}: {str(e)}"


def run_step(step_name: str, fn: Callable[[], object]) -> tuple[bool, str | None]:
    """執行單一步驟，回傳 (成功與否, 錯誤訊息)。"""
    result, err = run_step_value(step_name, fn)
    return result is not None, err


def determine_status(results: dict[str, bool]) -> str:
    """根據各步驟結果判定 job 整體狀態。"""
    values = list(results.values())
    if all(values):
        return "completed"
    elif any(values):
        return "partial"
    else:
        return "failed"
