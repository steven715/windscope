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
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
