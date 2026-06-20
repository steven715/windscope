"""回補指定日期範圍的歷史資料。"""

import logging
import time

from db.connection import get_connection
from jobs.after_close import run_after_close
from jobs.after_night import run_after_night
from jobs.chip_collect import run_chip_collect
from utils.trading_calendar import iter_trading_days

logger = logging.getLogger(__name__)


def run_backfill(start_date: str, end_date: str,
                 db_path: str | None = None) -> dict:
    """
    回補指定日期範圍的歷史資料。

    限制：
    - 回補 after_close（市場面）、after_night、chip_collect（個股收盤＋籌碼指標）
      ——這些有歷史可查；chip_collect 連帶補回 __PRICE_ONLY__ 收盤價（MA20 基礎）。
    - 不回補 before_open 的即時匯率（08:45 報價、台銀牌價只有即時值，無法回補歷史）
    - chip 分點明細需 FinMind token 或 /chip-import CSV，未設則該步略過。

    日期從舊到新執行，因為 integration 計算依賴歷史資料。
    chip_collect 以函式直呼，不受其「排程啟用/停用」開關影響（啟用只管自動排程）。
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

        # 收盤後市場面 + 夜盤後 + 籌碼分點（個股收盤＋分點＋算籌碼指標，自含 compute）
        ac_result = run_after_close(date, db_path)
        an_result = run_after_night(date, db_path)
        cc_result = run_chip_collect(date, db_path)

        # 合併三個 job 的結果
        merged_results = {}
        merged_errors = []
        for r in (ac_result, an_result, cc_result):
            merged_results.update(r.get("results", {}))
            merged_errors.extend(r.get("errors", []))

        # 補算 fx / futures 衍生指標（chip 指標已由 chip_collect 內部計算）。
        # FX 因缺 quote_0845 會產出 NULL delta，正常。
        try:
            with get_connection(db_path) as conn:
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
