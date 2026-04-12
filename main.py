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
        if success:
            # 讀出並顯示摘要
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
    else:
        print(f"Unknown collect target: {target}")
        sys.exit(1)


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
        "target", choices=["institutional"], help="Data target to collect"
    )
    collect_parser.add_argument(
        "--date",
        default=None,
        help="Date to collect (YYYY-MM-DD, default: today)",
    )

    args = parser.parse_args()

    if args.command == "init-db":
        cmd_init_db(args)
    elif args.command == "collect":
        if args.date is None:
            from datetime import date

            args.date = date.today().isoformat()
        cmd_collect(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
