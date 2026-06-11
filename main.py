import argparse
import logging
import os
import sqlite3
import sys

from config import settings
from db.connection import get_connection
from db.schema import create_all_tables, import_broker_tags, import_watchlist
from utils.logger import setup_logging

logger = logging.getLogger(__name__)


def cmd_init_db(args: argparse.Namespace) -> None:
    """建表 + 匯入 broker_tags + watchlist。"""
    os.makedirs(os.path.dirname(settings.DB_PATH) or ".", exist_ok=True)

    with get_connection() as conn:
        create_all_tables(conn)
        bt_count = import_broker_tags(conn)
        wl_count = import_watchlist(conn)

    print(f"Database initialized: {settings.DB_PATH}")
    print(f"  broker_tags: {bt_count} entries imported")
    print(f"  watchlist:   {wl_count} entries imported")


def cmd_collect(args: argparse.Namespace) -> None:
    """收集指定資料。"""
    target = args.target
    date = args.date

    if target == "institutional":
        from collectors.twse import TWSECollector

        collector = TWSECollector()
        success = collector.run(date)
        # 舊的 institutional-only 模式回傳 dict，取 institutional 結果
        if isinstance(success, dict):
            success = success.get("institutional", False)
        if success:
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT foreign_net, trust_net, dealer_net, total_net "
                    "FROM raw_institutional WHERE date = ?",
                    (date,),
                ).fetchone()
            if row:
                print(f"Institutional data for {date}:")
                print(f"  Foreign net: {row[0]:>15,.0f}")
                print(f"  Trust net:   {row[1]:>15,.0f}")
                print(f"  Dealer net:  {row[2]:>15,.0f}")
                print(f"  Total net:   {row[3]:>15,.0f}")
        else:
            print(f"Failed to collect institutional data for {date}")
            sys.exit(1)

    elif target == "twse":
        from collectors.twse import TWSECollector

        collector = TWSECollector()
        results = collector.run(date)
        print(f"TWSE collection for {date}:")
        for task, ok in results.items():
            status = "OK" if ok else "FAILED/NO DATA"
            print(f"  {task}: {status}")
        # 顯示 DB 中的摘要
        _print_twse_summary(date)

    elif target == "taifex":
        from collectors.taifex import TAIFEXCollector

        collector = TAIFEXCollector()
        results = collector.run(date)
        print(f"TAIFEX collection for {date}:")
        for task, ok in results.items():
            status = "OK" if ok else "FAILED/NO DATA (stub)"
            print(f"  {task}: {status}")

    elif target == "fx":
        from collectors.fx import FXCollector

        collector = FXCollector()
        results = collector.run(date)
        print(f"FX collection for {date}:")
        for task, ok in results.items():
            status = "OK" if ok else "FAILED/NO DATA (stub)"
            print(f"  {task}: {status}")

    else:
        print(f"Unknown collect target: {target}")
        sys.exit(1)


def cmd_import_chip(args: argparse.Namespace) -> None:
    """從 CSV 匯入分點籌碼資料。"""
    from collectors.chip import ChipCollector

    collector = ChipCollector()
    count = collector.import_from_csv(args.csv_path)
    print(f"Imported {count} rows from {args.csv_path}")


def cmd_compute(args: argparse.Namespace) -> None:
    """計算指定日期的所有衍生指標。"""
    import json

    from integration.chip_metrics import compute_chip_metrics
    from integration.futures_metrics import compute_futures_metrics
    from integration.fx_metrics import compute_fx_metrics

    date = args.date
    print(f"Computing metrics for {date}...")
    results = {}

    with get_connection() as conn:
        # FX
        try:
            fx = compute_fx_metrics(date, conn)
            results["fx"] = True
            if fx:
                print(f"  [FX] TWD delta: {fx['fx_delta_twd']}, "
                      f"direction: {fx['fx_direction']}, "
                      f"asia_sync: {fx['fx_asia_sync']}")
            else:
                print("  [FX] No data available")
        except Exception as e:
            results["fx"] = False
            logger.error("FX metrics failed: %s", e)
            print(f"  [FX] FAILED: {e}")

        # Futures
        try:
            fut = compute_futures_metrics(date, conn)
            results["futures"] = True
            if fut:
                print(f"  [Futures] spread: {fut['futures_spread']}, "
                      f"adjusted: {fut['futures_spread_adjusted']}, "
                      f"volume_ratio: {fut['futures_volume_ratio']}")
            else:
                print("  [Futures] No data available")
        except Exception as e:
            results["futures"] = False
            logger.error("Futures metrics failed: %s", e)
            print(f"  [Futures] FAILED: {e}")

        # Chip
        try:
            chip = compute_chip_metrics(date, conn)
            results["chip"] = True
            print(f"  [Chip] {len(chip)} records computed")
        except Exception as e:
            results["chip"] = False
            logger.error("Chip metrics failed: %s", e)
            print(f"  [Chip] FAILED: {e}")

    print(f"\nResults: {results}")


def cmd_query(args: argparse.Namespace) -> None:
    """查詢衍生指標。"""
    import json

    query_type = args.query_type
    date = args.date

    if query_type == "daily":
        _query_daily(date)
    elif query_type == "stock":
        stock_id = args.stock_id
        if not stock_id:
            print("Error: query stock requires a stock_id argument")
            sys.exit(1)
        _query_stock(date, stock_id)
    elif query_type == "signal":
        _query_signal(date)
    elif query_type == "verification":
        _query_verification(date)
    else:
        print(f"Unknown query type: {query_type}")
        sys.exit(1)


def _query_signal(date: str) -> None:
    """查詢市場訊號與個股觀察訊號。"""
    import json

    with get_connection() as conn:
        row = conn.execute(
            "SELECT direction, confidence, fx_vote, futures_vote, "
            "       reasons, rule_version FROM signals WHERE date = ?",
            (date,),
        ).fetchone()
        stock_rows = conn.execute(
            "SELECT stock_id, broker_name, category, reasons "
            "FROM stock_signals WHERE date = ? ORDER BY stock_id",
            (date,),
        ).fetchall()

    if not row:
        print(f"No signal found for {date}")
        return

    direction, confidence, fx_vote, futures_vote, reasons, rule_version = row
    print(f"=== {date} Signal (rule {rule_version}) ===")
    print(f"方向: {direction}  信心: {confidence}/5")
    print(f"匯率票: {fx_vote}  期貨票: {futures_vote}")
    print("理由:")
    for r in json.loads(reasons):
        print(f"  · {r}")

    if stock_rows:
        print("\n[個股觀察訊號]")
        for stock_id, broker, category, reason in stock_rows:
            print(f"  {stock_id} {broker}: {category} — {reason}")


def _query_verification(date: str) -> None:
    """查詢單日驗證結果與近 20 日命中率。"""
    from integration.verification import get_verification_stats

    with get_connection() as conn:
        row = conn.execute(
            "SELECT predicted_direction, confidence, day_change_pct, "
            "       day_change_class, open_gap_pct, open_gap_class, "
            "       hit_day, hit_open FROM verifications WHERE date = ?",
            (date,),
        ).fetchone()
        stats = get_verification_stats(conn)

    if not row:
        print(f"No verification found for {date}")
    else:
        (pred, conf, day_pct, day_cls, gap_pct, gap_cls,
         hit_day, hit_open) = row
        print(f"=== {date} Verification ===")
        print(f"預測: {pred} (信心 {conf})")
        print(f"當日漲跌: {day_pct:+.2f}% ({day_cls}) → {'✓ 命中' if hit_day else '✗ 失誤'}")
        print(f"開盤跳空: {gap_pct:+.2f}% ({gap_cls}) → {'✓' if hit_open else '✗'}")

    if stats["total"] > 0:
        print(f"\n[近 {stats['total']} 日命中率]")
        print(f"  收盤基準: {stats['hit_day_rate']}%  跳空基準: {stats['hit_open_rate']}%")
        for conf_level in sorted(stats["by_confidence"], reverse=True):
            b = stats["by_confidence"][conf_level]
            print(f"  信心 {conf_level}: {b['rate']}% ({b['hits']}/{b['total']})")


def _format_amount(amount: float | None) -> str:
    """格式化金額，超過 1 億用「億」為單位。"""
    if amount is None:
        return "N/A"
    if abs(amount) >= 1e8:
        return f"{amount / 1e8:.2f} 億"
    elif abs(amount) >= 1e4:
        return f"{amount / 1e4:.0f} 萬"
    else:
        return f"{amount:,.0f}"


def _query_daily(date: str) -> None:
    """查詢 daily_metrics。"""
    import json

    with get_connection() as conn:
        row = conn.execute(
            "SELECT fx_delta_twd, fx_delta_cny, fx_delta_krw, "
            "       fx_direction, fx_asia_sync, fx_asia_detail, "
            "       futures_spread, futures_spread_adjusted, "
            "       futures_volume_ratio, oi_net_foreign, oi_delta "
            "FROM daily_metrics WHERE date = ?",
            (date,),
        ).fetchone()

    if not row:
        print(f"No daily metrics found for {date}")
        return

    (fx_twd, fx_cny, fx_krw, fx_dir, fx_sync, fx_detail,
     fut_spread, fut_adj, fut_ratio, oi_net, oi_delta) = row

    print(f"=== {date} Daily Metrics ===")

    # FX section
    print("[匯率]")
    if fx_twd is not None:
        print(f"  TWD delta:    {fx_twd:+.4f} ({fx_dir})")
    else:
        print("  TWD delta:    N/A")
    if fx_cny is not None:
        cny_dir = ""
        if fx_detail:
            detail = json.loads(fx_detail)
            cny_dir = f" ({detail.get('CNY', 'N/A')})"
        print(f"  CNY delta:    {fx_cny:+.4f}{cny_dir}")
    else:
        print("  CNY delta:    N/A")
    if fx_krw is not None:
        krw_dir = ""
        if fx_detail:
            detail = json.loads(fx_detail)
            krw_dir = f" ({detail.get('KRW', 'N/A')})"
        print(f"  KRW delta:    {fx_krw:+.4f}{krw_dir}")
    else:
        print("  KRW delta:    N/A")

    if fx_sync is not None:
        sync_label = "是" if fx_sync == 1 else "否"
        detail_str = ""
        if fx_detail:
            detail = json.loads(fx_detail)
            parts = [f"{k}:{v}" for k, v in detail.items() if v]
            detail_str = f" ({', '.join(parts)})"
        print(f"  亞幣同步:     {sync_label}{detail_str}")
    else:
        print("  亞幣同步:     N/A (資料不足)")

    # Futures section
    print("[期貨]")
    if fut_spread is not None:
        print(f"  夜盤價差:     {fut_spread:+.1f}")
    else:
        print("  夜盤價差:     N/A")
    if fut_adj is not None:
        ex_div = ""
        if fut_spread is not None and fut_adj != fut_spread:
            diff = round(fut_spread - fut_adj, 1)
            ex_div = f" (除息 {diff:.1f})"
        print(f"  調整後價差:   {fut_adj:+.1f}{ex_div}")
    else:
        print("  調整後價差:   N/A")
    if fut_ratio is not None:
        print(f"  夜盤量比:     {fut_ratio:.2f}x")
    else:
        print("  夜盤量比:     N/A")
    if oi_net is not None:
        print(f"  外資未平倉:   {oi_net:,}")
    else:
        print("  外資未平倉:   N/A (資料不可用)")
    if oi_delta is not None:
        print(f"  未平倉變化:   {oi_delta:+,}")
    else:
        print("  未平倉變化:   N/A")


def _query_stock(date: str, stock_id: str) -> None:
    """查詢個股籌碼指標。"""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT dsm.broker_name, dsm.net_amount, dsm.consecutive_days, "
            "       dsm.price_vs_ma20, dsm.price_zone, dsm.both_sides_flag, "
            "       dsm.broker_type, rc.stock_name "
            "FROM daily_stock_metrics dsm "
            "LEFT JOIN raw_chip rc ON dsm.date = rc.date "
            "  AND dsm.stock_id = rc.stock_id AND dsm.broker_name = rc.broker_name "
            "WHERE dsm.date = ? AND dsm.stock_id = ?",
            (date, stock_id),
        ).fetchall()

    if not rows:
        print(f"No stock metrics found for {stock_id} on {date}")
        return

    stock_name = rows[0][7] or stock_id
    print(f"=== {date} {stock_name}({stock_id}) ===")

    for row in rows:
        (broker, net_amount, consec, ma20_pct, zone,
         both_sides, broker_type, _) = row

        type_label = f" [{broker_type}]" if broker_type else ""
        print(f"  {broker}{type_label}")
        print(f"    買超金額:    {_format_amount(net_amount)}")

        if consec is not None and consec != 0:
            direction = "買超" if consec > 0 else "賣超"
            print(f"    連續{direction}:    {abs(consec)} 天")
        else:
            print(f"    連續買賣:    無連續")

        if ma20_pct is not None:
            print(f"    股價位置:    {zone} (MA20 {ma20_pct:+.1f}%)")
        else:
            print(f"    股價位置:    N/A")

        both_label = "是" if both_sides else "否"
        print(f"    雙邊交易:    {both_label}")


def cmd_run(args: argparse.Namespace) -> None:
    """執行完整 job（after-close / after-night / before-open）。"""
    job_name = args.job_name
    date = args.date

    if job_name == "after-close":
        from jobs.after_close import run_after_close

        result = run_after_close(date)
    elif job_name == "after-night":
        from jobs.after_night import run_after_night

        result = run_after_night(date)
    elif job_name == "before-open":
        from jobs.before_open import run_before_open

        result = run_before_open(date)
    elif job_name == "verify-close":
        from jobs.verify_close import run_verify_close

        result = run_verify_close(date)
    else:
        print(f"Unknown job: {job_name}")
        sys.exit(1)

    # 印出結果
    print(f"\n=== {job_name} for {date} ===")
    print(f"Status: {result['status']}")
    if result.get("results"):
        print("Steps:")
        for step, ok in result["results"].items():
            status_str = "OK" if ok else "FAILED/NO DATA"
            print(f"  {step}: {status_str}")
    if result.get("errors"):
        print("Errors:")
        for err in result["errors"]:
            print(f"  - {err}")

    # before-open 特有的 summary
    if result.get("summary"):
        print()
        print(result["summary"])

    # verify-close 特有的驗證結果
    if result.get("verification"):
        v = result["verification"]
        hit_label = "✓ 命中" if v["hit_day"] else "✗ 失誤"
        print()
        print(f"預測 {v['predicted_direction']} (信心 {v['confidence']}) "
              f"vs 實際 {v['day_change_class']} ({v['day_change_pct']:+.2f}%) → {hit_label}")
        print(f"開盤跳空 {v['open_gap_class']} ({v['open_gap_pct']:+.2f}%) "
              f"{'✓' if v['hit_open'] else '✗'}")


def cmd_backfill(args: argparse.Namespace) -> None:
    """回補歷史資料。"""
    from jobs.backfill import run_backfill

    result = run_backfill(args.from_date, args.to_date)

    print(f"\n=== Backfill {result['range']} ===")
    print(f"Total trading days: {result['total_days']}")
    print(f"  Completed: {result['completed']}")
    print(f"  Partial:   {result['partial']}")
    print(f"  Failed:    {result['failed']}")

    for date, detail in result["details"].items():
        status = detail["status"]
        print(f"\n  {date}: {status}")
        if detail.get("errors"):
            for err in detail["errors"]:
                print(f"    - {err}")


def cmd_watchlist(args: argparse.Namespace) -> None:
    """觀察名單管理。"""
    from db.watchlist import watchlist_add, watchlist_list, watchlist_remove

    action = args.action

    if action == "list":
        items = watchlist_list()
        if not items:
            print("觀察名單為空")
            return
        print(f"觀察名單（{len(items)} 檔）：")
        for item in items:
            print(
                f"  {item['stock_id']}  {item['stock_name']}  "
                f"{item['added_date']}  {item['reason']}"
            )

    elif action == "add":
        if not args.stock_id or not args.stock_name:
            print("Error: watchlist add requires stock_id and stock_name")
            sys.exit(1)
        reason = args.reason or ""
        ok = watchlist_add(args.stock_id, args.stock_name, reason)
        if ok:
            print(f"Added {args.stock_id} {args.stock_name}")

    elif action == "remove":
        if not args.stock_id:
            print("Error: watchlist remove requires stock_id")
            sys.exit(1)
        ok = watchlist_remove(args.stock_id)
        if ok:
            print(f"Removed {args.stock_id}")
        else:
            print(f"Not found: {args.stock_id}")

    else:
        print(f"Unknown watchlist action: {action}")
        sys.exit(1)


def _print_twse_summary(date: str) -> None:
    """印出 TWSE 收集後的 DB 摘要。"""
    with get_connection() as conn:
        # 三大法人
        row = conn.execute(
            "SELECT foreign_net, trust_net, dealer_net, total_net "
            "FROM raw_institutional WHERE date = ?",
            (date,),
        ).fetchone()
        if row:
            print(f"  --- Institutional ---")
            print(f"  Foreign net: {row[0]:>15,.0f}")
            print(f"  Trust net:   {row[1]:>15,.0f}")
            print(f"  Dealer net:  {row[2]:>15,.0f}")
            print(f"  Total net:   {row[3]:>15,.0f}")

        # 加權指數
        row = conn.execute(
            "SELECT spot_close FROM raw_futures WHERE date = ?",
            (date,),
        ).fetchone()
        if row and row[0]:
            print(f"  --- Spot Close ---")
            print(f"  TAIEX: {row[0]:>12,.2f}")

        # 除息
        row = conn.execute(
            "SELECT ex_dividend_points FROM raw_futures WHERE date = ?",
            (date,),
        ).fetchone()
        if row and row[0] is not None:
            print(f"  Ex-dividend points: {row[0]:.2f}")


def main() -> None:
    """CLI 進入點。"""
    setup_logging()

    parser = argparse.ArgumentParser(
        description="Pre-Market Intelligence System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init-db
    subparsers.add_parser("init-db", help="Initialize database and import config")

    # collect
    collect_parser = subparsers.add_parser("collect", help="Collect market data")
    collect_parser.add_argument(
        "target",
        choices=["institutional", "twse", "taifex", "fx"],
        help="Data target to collect",
    )
    collect_parser.add_argument(
        "--date",
        default=None,
        help="Date to collect (YYYY-MM-DD, default: today)",
    )

    # import-chip
    import_parser = subparsers.add_parser(
        "import-chip", help="Import chip data from CSV"
    )
    import_parser.add_argument(
        "csv_path", help="Path to the chip CSV file"
    )

    # compute
    compute_parser = subparsers.add_parser(
        "compute", help="Compute derived metrics for a date"
    )
    compute_parser.add_argument(
        "--date", default=None,
        help="Date to compute (YYYY-MM-DD, default: today)",
    )

    # run (job)
    run_parser = subparsers.add_parser(
        "run", help="Run a complete job (after-close, after-night, before-open, verify-close)"
    )
    run_parser.add_argument(
        "job_name",
        choices=["after-close", "after-night", "before-open", "verify-close"],
        help="Job to run",
    )
    run_parser.add_argument(
        "--date", default=None,
        help="Date (YYYY-MM-DD, default: today)",
    )

    # backfill
    backfill_parser = subparsers.add_parser(
        "backfill", help="Backfill historical data for a date range"
    )
    backfill_parser.add_argument(
        "--from", dest="from_date", required=True,
        help="Start date (YYYY-MM-DD)",
    )
    backfill_parser.add_argument(
        "--to", dest="to_date", required=True,
        help="End date (YYYY-MM-DD)",
    )

    # watchlist
    watchlist_parser = subparsers.add_parser(
        "watchlist", help="Manage watchlist"
    )
    watchlist_parser.add_argument(
        "action", choices=["list", "add", "remove"],
        help="Action: list, add, or remove",
    )
    watchlist_parser.add_argument(
        "stock_id", nargs="?", default=None,
        help="Stock ID (required for add/remove)",
    )
    watchlist_parser.add_argument(
        "stock_name", nargs="?", default=None,
        help="Stock name (required for add)",
    )
    watchlist_parser.add_argument(
        "reason", nargs="?", default=None,
        help="Reason for adding (optional)",
    )

    # query
    query_parser = subparsers.add_parser(
        "query", help="Query computed metrics"
    )
    query_parser.add_argument(
        "query_type", choices=["daily", "stock", "signal", "verification"],
        help="Query type: daily, stock, signal, or verification",
    )
    query_parser.add_argument(
        "date", help="Date to query (YYYY-MM-DD)",
    )
    query_parser.add_argument(
        "stock_id", nargs="?", default=None,
        help="Stock ID (required for 'stock' query)",
    )

    args = parser.parse_args()

    if args.command == "init-db":
        cmd_init_db(args)
    elif args.command == "collect":
        if args.date is None:
            from datetime import date

            args.date = date.today().isoformat()
        cmd_collect(args)
    elif args.command == "import-chip":
        cmd_import_chip(args)
    elif args.command == "compute":
        if args.date is None:
            from datetime import date

            args.date = date.today().isoformat()
        cmd_compute(args)
    elif args.command == "run":
        if args.date is None:
            from datetime import date

            args.date = date.today().isoformat()
        cmd_run(args)
    elif args.command == "backfill":
        cmd_backfill(args)
    elif args.command == "watchlist":
        cmd_watchlist(args)
    elif args.command == "query":
        cmd_query(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
