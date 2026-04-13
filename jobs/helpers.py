"""Job 共用工具：步驟執行、狀態判定。"""

import logging

logger = logging.getLogger(__name__)


def run_step(step_name: str, fn: callable) -> tuple[bool, str | None]:
    """執行單一步驟，回傳 (成功與否, 錯誤訊息)。"""
    try:
        result = fn()
        if result is None or result is False:
            return False, f"{step_name}: no data"
        return True, None
    except Exception as e:
        logger.error("%s failed: %s", step_name, e)
        return False, f"{step_name}: {str(e)}"


def determine_status(results: dict[str, bool]) -> str:
    """根據各步驟結果判定 job 整體狀態。"""
    values = list(results.values())
    if all(values):
        return "completed"
    elif any(values):
        return "partial"
    else:
        return "failed"
