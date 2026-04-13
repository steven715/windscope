"""回補指定日期範圍的歷史資料。"""

import logging
import time

from db.connection import get_connection
from jobs.after_close import run_after_close
from jobs.after_night import run_after_night
from utils.trading_calendar import iter_trading_days

logger = logging.getLogger(__name__)


def run_backfill(start_date: str, end_date: str,
                 db_path: str | None = None) -> dict:
    """
    回補指定日期範圍的歷史資料。

    限制：
    - 只回補 after_close 和 after_night 的資料（這些有歷史可查）
    - 不回補 before_open 的即時匯率（08:45 報價無法回補歷史）
    - 不回補 chip 分點資料（依賴 CSV 手動匯入）

    日期從舊到新執行，因為 integration 計算依賴歷史資料。
    """
    logger.info("run_backfill: %s ~ %s", start_date, end_date)

    trading_days = iter_trading_days(start_date, end_date)
    total = len(trading_days)

    details = {}
    completed = 0
    partial = 0
    failed = 0

    for i, date in enumerate(trading_days, 1):
        logger.info("Backfill [%d/%d]: %s", i, total, date)

        # 執行 after_close（不含 chip 分點）
        ac_result = run_after_close(date, db_path)

        # 執行 after_night
        an_result = run_after_night(date, db_path)

        # 合併兩個 job 的結果
        merged_results = {}
        merged_errors = []
        merged_results.update(ac_result.get("results", {}))
        merged_results.update(an_result.get("results", {}))
        merged_errors.extend(ac_result.get("errors", []))
        merged_errors.extend(an_result.get("errors", []))

        # 也跑 integration（FX 因缺 quote_0845 會產出 NULL delta，正常）
        try:
            with get_connection(db_path) as conn:
                from integration.chip_metrics import compute_chip_metrics
                from integration.futures_metrics import compute_futures_metrics
                from integration.fx_metrics import compute_fx_metrics

                try:
                    compute_fx_metrics(date, conn)
                except Exception as e:
                    logger.warning("Backfill FX integration for %s: %s", date, e)

                try:
                    compute_futures_metrics(date, conn)
                except Exception as e:
                    logger.warning("Backfill futures integration for %s: %s", date, e)

                try:
                    compute_chip_metrics(date, conn)
                except Exception as e:
                    logger.warning("Backfill chip integration for %s: %s", date, e)
        except Exception as e:
            logger.error("Backfill integration failed for %s: %s", date, e)

        # 判定狀態
        values = list(merged_results.values())
        if all(values):
            day_status = "completed"
            completed += 1
        elif any(values):
            day_status = "partial"
            partial += 1
        else:
            day_status = "failed"
            failed += 1

        details[date] = {
            "status": day_status,
            "results": merged_results,
            "errors": merged_errors,
        }

        # 禮貌延遲（最後一天不需要）
        if i < total:
            delay = 3
            logger.debug("Backfill: sleeping %ds before next date", delay)
            time.sleep(delay)

    result = {
        "range": f"{start_date} ~ {end_date}",
        "total_days": total,
        "completed": completed,
        "partial": partial,
        "failed": failed,
        "details": details,
    }

    logger.info(
        "Backfill done: %d total, %d completed, %d partial, %d failed",
        total, completed, partial, failed,
    )
    return result
