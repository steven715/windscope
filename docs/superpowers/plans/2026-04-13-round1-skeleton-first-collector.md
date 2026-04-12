# Round 1: Project Skeleton + First Collector — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the project skeleton and a working end-to-end pipeline: create DB tables, fetch TWSE institutional investors data, store in SQLite, verify via CLI.

**Architecture:** Layered structure — `config/` for settings, `db/` for schema and connection management, `collectors/` for data fetching with a BaseCollector ABC, `utils/` for shared HTTP and logging, `main.py` as CLI entry point. All HTTP goes through a single `http_get` wrapper. All DB access through a context-manager connection helper. Collectors accept injectable `db_path` for testability.

**Tech Stack:** Python 3.10+, SQLite (stdlib), requests, beautifulsoup4, pytest, argparse

---

## File Structure

| File | Responsibility |
|------|---------------|
| `requirements.txt` | Project dependencies |
| `.gitignore` | Ignored paths |
| `config/__init__.py` | Package marker |
| `config/settings.py` | Centralized constants (DB path, HTTP config) |
| `config/watchlist.json` | Stock watchlist data |
| `config/broker_tags.json` | Broker classification tags |
| `utils/__init__.py` | Package marker |
| `utils/logger.py` | Logging setup (console + file handler) |
| `utils/http_client.py` | Unified HTTP GET with retry, delay, UA |
| `db/__init__.py` | Package marker |
| `db/connection.py` | Context-manager DB connection |
| `db/schema.py` | Table creation + JSON import functions |
| `collectors/__init__.py` | Package marker |
| `collectors/base.py` | BaseCollector ABC |
| `collectors/twse.py` | TWSE institutional investors collector |
| `integration/__init__.py` | Package marker (empty for Round 1) |
| `jobs/__init__.py` | Package marker (empty for Round 1) |
| `main.py` | CLI entry point (init-db, collect) |
| `tests/__init__.py` | Package marker |
| `tests/conftest.py` | Shared pytest fixtures |
| `tests/fixtures/twse/bfi82u_20260408.json` | Normal response fixture |
| `tests/fixtures/twse/bfi82u_20260412_holiday.json` | Holiday response fixture |
| `tests/test_db/test_schema.py` | DB schema + import tests |
| `tests/test_collectors/test_twse.py` | TWSE collector tests |

---

## Task 1: Project scaffolding — config, gitignore, requirements

**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`
- Create: `config/__init__.py`
- Create: `config/settings.py`
- Create: `config/watchlist.json`
- Create: `config/broker_tags.json`
- Create: `collectors/__init__.py`
- Create: `integration/__init__.py`
- Create: `jobs/__init__.py`
- Create: `utils/__init__.py`
- Create: `tests/__init__.py`
- Create: `data/` (directory)
- Create: `logs/` (directory)

- [ ] **Step 1: Create requirements.txt**

```
requests>=2.31.0
beautifulsoup4>=4.12.0
pytest>=7.0.0
```

- [ ] **Step 2: Create .gitignore**

```
data/
logs/
__pycache__/
.pytest_cache/
*.pyc
*.egg-info/
.env
```

- [ ] **Step 3: Create config/settings.py**

```python
import os

# DB
DB_PATH = os.environ.get("PREMARKET_DB", "data/premarket.db")

# HTTP
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_DELAY_MIN = 1.0
HTTP_DELAY_MAX = 3.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
```

- [ ] **Step 4: Create config/watchlist.json**

```json
[
  {"stock_id": "2330", "stock_name": "台積電", "added_date": "2026-04-08", "reason": "權值股常態追蹤"},
  {"stock_id": "2409", "stock_name": "友達", "added_date": "2026-04-08", "reason": "外資反手大買"}
]
```

- [ ] **Step 5: Create config/broker_tags.json**

```json
[
  {"broker_name": "兆豐-嘉義", "broker_type": "swing", "notes": "長線主力，連買3-5天後抱1-2個月"},
  {"broker_name": "凱基-台北", "broker_type": "day_trade", "notes": "隔日沖高手，今買明賣"},
  {"broker_name": "元大-土城永寧", "broker_type": "day_trade", "notes": "隔日沖"},
  {"broker_name": "永豐金-萬盛", "broker_type": "swing", "notes": "波段王"},
  {"broker_name": "港商麥格理", "broker_type": "hedge", "notes": "避險，大買可能掩護現貨賣超"},
  {"broker_name": "美銀", "broker_type": "hedge", "notes": "避險"}
]
```

- [ ] **Step 6: Create empty __init__.py files and directories**

Create `config/__init__.py`, `collectors/__init__.py`, `integration/__init__.py`, `jobs/__init__.py`, `utils/__init__.py`, `tests/__init__.py` — all empty files. Create `data/` and `logs/` directories (with `.gitkeep` not needed since they're in `.gitignore`).

- [ ] **Step 7: Install dependencies and commit**

```bash
pip install -r requirements.txt
git init
git add requirements.txt .gitignore config/ collectors/__init__.py integration/__init__.py jobs/__init__.py utils/__init__.py tests/__init__.py
git commit -m "chore: Round 1 scaffolding — config, requirements, gitignore, package structure"
```

---

## Task 2: Logging utility

**Files:**
- Create: `utils/logger.py`

- [ ] **Step 1: Write utils/logger.py**

```python
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
```

- [ ] **Step 2: Commit**

```bash
git add utils/logger.py
git commit -m "feat: add logging utility with console and file handlers"
```

---

## Task 3: HTTP client utility

**Files:**
- Create: `utils/http_client.py`
- Test: `tests/test_utils/` (deferred — tested indirectly via collector tests)

- [ ] **Step 1: Write utils/http_client.py**

```python
import logging
import random
import time

import requests

from config import settings

logger = logging.getLogger(__name__)


def http_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int | None = None,
    encoding: str | None = None,
) -> requests.Response:
    """統一 GET 請求，內建 User-Agent、timeout、指數退避 retry、random delay。"""
    timeout = timeout or settings.HTTP_TIMEOUT
    request_headers = {"User-Agent": settings.USER_AGENT}
    if headers:
        request_headers.update(headers)

    last_exception: Exception | None = None

    for attempt in range(1, settings.HTTP_RETRIES + 1):
        # 禮貌延遲
        delay = random.uniform(settings.HTTP_DELAY_MIN, settings.HTTP_DELAY_MAX)
        time.sleep(delay)

        try:
            resp = requests.get(
                url, params=params, headers=request_headers, timeout=timeout
            )
            if encoding:
                resp.encoding = encoding
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exception = e
            body_preview = ""
            if hasattr(e, "response") and e.response is not None:
                body_preview = e.response.text[:200]
            logger.warning(
                "HTTP GET attempt %d/%d failed: %s | URL: %s | body: %s",
                attempt,
                settings.HTTP_RETRIES,
                e,
                url,
                body_preview,
            )
            if attempt < settings.HTTP_RETRIES:
                backoff = delay * (2 ** (attempt - 1))
                time.sleep(backoff)

    logger.error("HTTP GET failed after %d retries: %s", settings.HTTP_RETRIES, url)
    raise last_exception
```

- [ ] **Step 2: Commit**

```bash
git add utils/http_client.py
git commit -m "feat: add unified HTTP client with retry, delay, and logging"
```

---

## Task 4: DB connection manager

**Files:**
- Create: `db/connection.py`
- Create: `db/__init__.py`

- [ ] **Step 1: Write db/__init__.py (empty)**

- [ ] **Step 2: Write db/connection.py**

```python
import logging
import sqlite3
from contextlib import contextmanager

from config import settings

logger = logging.getLogger(__name__)


@contextmanager
def get_connection(db_path: str | None = None):
    """Context manager，自動 commit / rollback / close。測試時傳 ':memory:'。"""
    db_path = db_path or settings.DB_PATH
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

- [ ] **Step 3: Commit**

```bash
git add db/
git commit -m "feat: add DB connection context manager"
```

---

## Task 5: DB schema — TDD

**Files:**
- Create: `db/schema.py`
- Create: `tests/conftest.py`
- Create: `tests/test_db/__init__.py`
- Create: `tests/test_db/test_schema.py`

- [ ] **Step 1: Write tests/conftest.py**

```python
import sqlite3

import pytest

from db.schema import create_all_tables


@pytest.fixture
def memory_db():
    """提供 in-memory SQLite，已建好所有表。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    yield conn
    conn.close()
```

- [ ] **Step 2: Write failing tests in tests/test_db/test_schema.py**

```python
import json
import sqlite3
from pathlib import Path

from db.schema import create_all_tables, import_broker_tags, import_watchlist


class TestCreateAllTables:
    def test_creates_all_expected_tables(self, memory_db):
        """create_all_tables 應建出所有預期的表。"""
        tables = memory_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}

        expected = {
            "raw_fx",
            "raw_futures",
            "raw_chip",
            "raw_institutional",
            "broker_tags",
            "watchlist",
            "daily_metrics",
            "daily_stock_metrics",
        }
        assert expected.issubset(table_names)

    def test_idempotent(self):
        """重複執行 create_all_tables 不應報錯。"""
        conn = sqlite3.connect(":memory:")
        create_all_tables(conn)
        create_all_tables(conn)  # 第二次不應 raise
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        assert len(tables) >= 8
        conn.close()


class TestImportBrokerTags:
    def test_import_broker_tags(self, memory_db, tmp_path):
        """import_broker_tags 能正確匯入 JSON 資料。"""
        tags = [
            {"broker_name": "兆豐-嘉義", "broker_type": "swing", "notes": "長線"},
            {"broker_name": "凱基-台北", "broker_type": "day_trade", "notes": "隔日沖"},
        ]
        json_path = tmp_path / "broker_tags.json"
        json_path.write_text(json.dumps(tags, ensure_ascii=False), encoding="utf-8")

        count = import_broker_tags(memory_db, str(json_path))
        assert count == 2

        rows = memory_db.execute("SELECT * FROM broker_tags").fetchall()
        assert len(rows) == 2

    def test_import_broker_tags_idempotent(self, memory_db, tmp_path):
        """重複匯入不應報錯，且資料為最新值。"""
        tags = [{"broker_name": "測試券商", "broker_type": "swing", "notes": "v1"}]
        json_path = tmp_path / "broker_tags.json"
        json_path.write_text(json.dumps(tags, ensure_ascii=False), encoding="utf-8")

        import_broker_tags(memory_db, str(json_path))

        tags[0]["notes"] = "v2"
        json_path.write_text(json.dumps(tags, ensure_ascii=False), encoding="utf-8")
        import_broker_tags(memory_db, str(json_path))

        row = memory_db.execute(
            "SELECT notes FROM broker_tags WHERE broker_name = '測試券商'"
        ).fetchone()
        assert row[0] == "v2"


class TestImportWatchlist:
    def test_import_watchlist(self, memory_db, tmp_path):
        """import_watchlist 能正確匯入 JSON 資料。"""
        stocks = [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "added_date": "2026-04-08",
                "reason": "權值股",
            }
        ]
        json_path = tmp_path / "watchlist.json"
        json_path.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")

        count = import_watchlist(memory_db, str(json_path))
        assert count == 1

        rows = memory_db.execute("SELECT * FROM watchlist").fetchall()
        assert len(rows) == 1

    def test_import_watchlist_idempotent(self, memory_db, tmp_path):
        """重複匯入是冪等的。"""
        stocks = [
            {
                "stock_id": "2330",
                "stock_name": "台積電",
                "added_date": "2026-04-08",
                "reason": "v1",
            }
        ]
        json_path = tmp_path / "watchlist.json"
        json_path.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")

        import_watchlist(memory_db, str(json_path))

        stocks[0]["reason"] = "v2"
        json_path.write_text(json.dumps(stocks, ensure_ascii=False), encoding="utf-8")
        import_watchlist(memory_db, str(json_path))

        row = memory_db.execute(
            "SELECT reason FROM watchlist WHERE stock_id = '2330'"
        ).fetchone()
        assert row[0] == "v2"
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_db/test_schema.py -v
```

Expected: FAIL (db.schema module does not exist yet)

- [ ] **Step 4: Implement db/schema.py**

```python
import json
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_fx (
    date TEXT NOT NULL,
    currency_pair TEXT NOT NULL,
    close_16 REAL,
    quote_0845 REAL,
    ny_close REAL,
    collected_at TEXT,
    PRIMARY KEY (date, currency_pair)
);

CREATE TABLE IF NOT EXISTS raw_futures (
    date TEXT PRIMARY KEY,
    night_close REAL,
    night_volume INTEGER,
    spot_close REAL,
    oi_net_foreign INTEGER,
    ex_dividend_points REAL,
    ftse_tw_close REAL,
    sp500_close REAL,
    collected_at TEXT
);

CREATE TABLE IF NOT EXISTS raw_chip (
    date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    broker_name TEXT NOT NULL,
    buy_volume INTEGER,
    sell_volume INTEGER,
    net_volume INTEGER,
    close_price REAL,
    collected_at TEXT,
    PRIMARY KEY (date, stock_id, broker_name)
);

CREATE TABLE IF NOT EXISTS raw_institutional (
    date TEXT PRIMARY KEY,
    foreign_buy REAL,
    foreign_sell REAL,
    foreign_net REAL,
    trust_buy REAL,
    trust_sell REAL,
    trust_net REAL,
    dealer_buy REAL,
    dealer_sell REAL,
    dealer_net REAL,
    total_net REAL,
    collected_at TEXT
);

CREATE TABLE IF NOT EXISTS broker_tags (
    broker_name TEXT PRIMARY KEY,
    broker_type TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS watchlist (
    stock_id TEXT PRIMARY KEY,
    stock_name TEXT,
    added_date TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT PRIMARY KEY,
    fx_delta_twd REAL,
    fx_delta_cny REAL,
    fx_delta_krw REAL,
    fx_direction TEXT,
    fx_asia_sync INTEGER,
    fx_asia_detail TEXT,
    futures_spread REAL,
    futures_spread_adjusted REAL,
    futures_volume_ratio REAL,
    oi_net_foreign INTEGER,
    oi_delta INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS daily_stock_metrics (
    date TEXT NOT NULL,
    stock_id TEXT NOT NULL,
    broker_name TEXT NOT NULL,
    net_amount REAL,
    consecutive_days INTEGER,
    price_vs_ma20 REAL,
    price_zone TEXT,
    both_sides_flag INTEGER,
    broker_type TEXT,
    PRIMARY KEY (date, stock_id, broker_name)
);
"""


def create_all_tables(conn: sqlite3.Connection) -> None:
    """建立所有 Phase 1 的表。可重複執行（CREATE TABLE IF NOT EXISTS）。"""
    conn.executescript(_SCHEMA_SQL)
    logger.info("All tables created (or already exist)")


def import_broker_tags(
    conn: sqlite3.Connection, json_path: str | None = None
) -> int:
    """從 broker_tags.json 匯入分點標籤，回傳匯入筆數。"""
    json_path = json_path or str(
        Path(__file__).resolve().parent.parent / "config" / "broker_tags.json"
    )
    with open(json_path, encoding="utf-8") as f:
        tags = json.load(f)

    for tag in tags:
        conn.execute(
            "INSERT OR REPLACE INTO broker_tags (broker_name, broker_type, notes) "
            "VALUES (?, ?, ?)",
            (tag["broker_name"], tag["broker_type"], tag["notes"]),
        )
    conn.commit()
    logger.info("Imported %d broker tags", len(tags))
    return len(tags)


def import_watchlist(
    conn: sqlite3.Connection, json_path: str | None = None
) -> int:
    """從 watchlist.json 匯入觀察名單，回傳匯入筆數。"""
    json_path = json_path or str(
        Path(__file__).resolve().parent.parent / "config" / "watchlist.json"
    )
    with open(json_path, encoding="utf-8") as f:
        stocks = json.load(f)

    for stock in stocks:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (stock_id, stock_name, added_date, reason) "
            "VALUES (?, ?, ?, ?)",
            (
                stock["stock_id"],
                stock["stock_name"],
                stock["added_date"],
                stock["reason"],
            ),
        )
    conn.commit()
    logger.info("Imported %d watchlist entries", len(stocks))
    return len(stocks)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_db/test_schema.py -v
```

Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add db/schema.py tests/conftest.py tests/__init__.py tests/test_db/
git commit -m "feat: add DB schema with all Phase 1 tables, broker_tags/watchlist import, and tests"
```

---

## Task 6: BaseCollector ABC

**Files:**
- Create: `collectors/base.py`

- [ ] **Step 1: Write collectors/base.py**

```python
import logging
from abc import ABC, abstractmethod

from config import settings

logger = logging.getLogger(__name__)


class BaseCollector(ABC):
    """所有 collector 的抽象基底類別。"""

    def __init__(self, db_path: str | None = None):
        """db_path 可注入，預設從 settings 讀取。"""
        self.db_path = db_path or settings.DB_PATH

    @abstractmethod
    def collect(self, date: str) -> dict | None:
        """收集指定日期的資料。成功回傳 dict，失敗回傳 None。"""

    @abstractmethod
    def save(self, date: str, data: dict) -> None:
        """存入 SQLite raw table。用 INSERT OR REPLACE。"""

    def run(self, date: str) -> bool:
        """collect -> save。成功回傳 True，失敗回傳 False 並 log error。"""
        logger.info("%s: starting for %s", self.__class__.__name__, date)
        try:
            data = self.collect(date)
            if data is None:
                logger.warning("%s: no data for %s", self.__class__.__name__, date)
                return False
            self.save(date, data)
            logger.info("%s: saved data for %s", self.__class__.__name__, date)
            return True
        except Exception as e:
            logger.error("%s failed for %s: %s", self.__class__.__name__, date, e)
            return False
```

- [ ] **Step 2: Commit**

```bash
git add collectors/base.py
git commit -m "feat: add BaseCollector ABC with run/collect/save pattern"
```

---

## Task 7: TWSE Collector + test fixtures — TDD

**Files:**
- Create: `tests/fixtures/twse/bfi82u_20260408.json`
- Create: `tests/fixtures/twse/bfi82u_20260412_holiday.json`
- Create: `tests/test_collectors/__init__.py`
- Create: `tests/test_collectors/test_twse.py`
- Create: `collectors/twse.py`

- [ ] **Step 1: Create test fixture — normal response**

Try to fetch a real response first. If the request fails (date in the future, blocked, etc.), create a realistic fixture manually.

```bash
curl -s "https://www.twse.com.tw/rwd/zh/fund/BFI82U?date=20260408&response=json"
```

If this returns a valid response with `"stat": "OK"`, save it directly. Otherwise, create `tests/fixtures/twse/bfi82u_20260408.json`:

```json
{
  "stat": "OK",
  "title": "115年04月08日 三大法人買賣金額統計表",
  "date": "20260408",
  "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
  "data": [
    ["自營商(自行買賣)", "3,015,672,980", "2,814,353,670", "201,319,310"],
    ["自營商(避險)", "7,248,913,450", "6,987,241,120", "261,672,330"],
    ["投信", "4,562,187,300", "3,891,024,560", "671,162,740"],
    ["外資及陸資(不含外資自營商)", "58,723,456,100", "52,184,231,800", "6,539,224,300"],
    ["外資自營商", "125,430,200", "98,210,150", "27,220,050"],
    ["合計", "73,675,660,030", "65,975,061,300", "7,700,598,730"]
  ],
  "notes": ["說明：..."]
}
```

- [ ] **Step 2: Create test fixture — holiday response**

Create `tests/fixtures/twse/bfi82u_20260412_holiday.json`:

```json
{
  "stat": "很抱歉，沒有符合條件的資料!",
  "title": ""
}
```

- [ ] **Step 3: Write failing tests in tests/test_collectors/test_twse.py**

```python
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from collectors.twse import TWSECollector
from db.schema import create_all_tables

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "twse"


@pytest.fixture
def twse_collector(tmp_path):
    """建立 TWSECollector，使用 tmp_path 的 DB。"""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    create_all_tables(conn)
    conn.close()
    return TWSECollector(db_path=db_path)


def _load_fixture(filename: str) -> dict:
    return json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))


class TestCollectInstitutional:
    def test_parse_normal_response(self, twse_collector):
        """正常回應能正確 parse 出三大法人買賣超。"""
        fixture = _load_fixture("bfi82u_20260408.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            data = twse_collector.collect("2026-04-08")

        assert data is not None
        assert "foreign_buy" in data
        assert "foreign_sell" in data
        assert "foreign_net" in data
        assert "trust_net" in data
        assert "dealer_net" in data
        assert "total_net" in data
        # 驗證數字去逗號後正確
        assert isinstance(data["foreign_buy"], (int, float))
        assert data["foreign_buy"] > 0

    def test_holiday_returns_none(self, twse_collector):
        """非交易日回傳 None。"""
        fixture = _load_fixture("bfi82u_20260412_holiday.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            data = twse_collector.collect("2026-04-12")

        assert data is None

    def test_comma_number_parsing(self, twse_collector):
        """帶逗號的金額字串能正確轉成數字。"""
        fixture = _load_fixture("bfi82u_20260408.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            data = twse_collector.collect("2026-04-08")

        # 確保所有金額欄位都是數值
        for key in [
            "foreign_buy", "foreign_sell", "foreign_net",
            "trust_buy", "trust_sell", "trust_net",
            "dealer_buy", "dealer_sell", "dealer_net",
            "total_net",
        ]:
            assert isinstance(data[key], (int, float)), f"{key} should be numeric"


class TestSaveAndIdempotent:
    def test_save_writes_to_db(self, twse_collector):
        """save 能正確寫入 raw_institutional。"""
        data = {
            "foreign_buy": 58723456100,
            "foreign_sell": 52184231800,
            "foreign_net": 6539224300,
            "trust_buy": 4562187300,
            "trust_sell": 3891024560,
            "trust_net": 671162740,
            "dealer_buy": 10264586430,
            "dealer_sell": 9801594790,
            "dealer_net": 462991640,
            "total_net": 7700598730,
        }
        twse_collector.save("2026-04-08", data)

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT foreign_net, total_net FROM raw_institutional WHERE date = '2026-04-08'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == 6539224300
        assert row[1] == 7700598730

    def test_save_idempotent(self, twse_collector):
        """同一天存兩次不報錯，且資料為最新值。"""
        data1 = {
            "foreign_buy": 100, "foreign_sell": 50, "foreign_net": 50,
            "trust_buy": 100, "trust_sell": 50, "trust_net": 50,
            "dealer_buy": 100, "dealer_sell": 50, "dealer_net": 50,
            "total_net": 150,
        }
        data2 = {
            "foreign_buy": 200, "foreign_sell": 60, "foreign_net": 140,
            "trust_buy": 200, "trust_sell": 60, "trust_net": 140,
            "dealer_buy": 200, "dealer_sell": 60, "dealer_net": 140,
            "total_net": 420,
        }

        twse_collector.save("2026-04-08", data1)
        twse_collector.save("2026-04-08", data2)

        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT total_net FROM raw_institutional WHERE date = '2026-04-08'"
        ).fetchone()
        conn.close()

        assert row[0] == 420


class TestRunFlow:
    def test_http_failure_returns_false(self, twse_collector):
        """HTTP 失敗時 run() 回傳 False 而非 crash。"""
        with patch(
            "collectors.twse.http_get",
            side_effect=Exception("Connection timeout"),
        ):
            result = twse_collector.run("2026-04-08")

        assert result is False

    def test_run_success(self, twse_collector):
        """run() 正常流程：collect -> save -> True。"""
        fixture = _load_fixture("bfi82u_20260408.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture

        with patch("collectors.twse.http_get", return_value=mock_resp):
            result = twse_collector.run("2026-04-08")

        assert result is True

        # 確認資料有寫入
        conn = sqlite3.connect(twse_collector.db_path)
        row = conn.execute(
            "SELECT foreign_net FROM raw_institutional WHERE date = '2026-04-08'"
        ).fetchone()
        conn.close()
        assert row is not None
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
pytest tests/test_collectors/test_twse.py -v
```

Expected: FAIL (collectors.twse module does not exist)

- [ ] **Step 5: Implement collectors/twse.py**

```python
import logging
from datetime import datetime

from collectors.base import BaseCollector
from db.connection import get_connection
from utils.http_client import http_get

logger = logging.getLogger(__name__)

TWSE_BFI82U_URL = "https://www.twse.com.tw/rwd/zh/fund/BFI82U"


def _parse_amount(s: str) -> float:
    """去除逗號並轉為 float。處理負數（可能帶括號或負號）。"""
    s = s.strip().replace(",", "")
    # 有些負數用括號表示
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s)


class TWSECollector(BaseCollector):
    """證交所三大法人買賣超 collector。"""

    def collect(self, date: str) -> dict | None:
        """收集指定日期的三大法人買賣超。date 格式：YYYY-MM-DD。"""
        date_param = date.replace("-", "")  # -> YYYYMMDD
        resp = http_get(TWSE_BFI82U_URL, params={"date": date_param, "response": "json"})
        data = resp.json()

        if data.get("stat") != "OK":
            logger.info("BFI82U: no data for %s (stat=%s)", date, data.get("stat"))
            return None

        rows = data.get("data", [])
        if not rows:
            logger.warning("BFI82U: stat=OK but data is empty for %s", date)
            return None

        result = {}

        for row in rows:
            name = row[0].strip()
            buy = _parse_amount(row[1])
            sell = _parse_amount(row[2])
            net = _parse_amount(row[3])

            if "外資及陸資" in name and "自營商" not in name:
                result["foreign_buy"] = buy
                result["foreign_sell"] = sell
                result["foreign_net"] = net
            elif name == "投信":
                result["trust_buy"] = buy
                result["trust_sell"] = sell
                result["trust_net"] = net
            elif "自營商" in name and "自行買賣" in name:
                dealer_self_buy = buy
                dealer_self_sell = sell
                dealer_self_net = net
            elif "自營商" in name and "避險" in name:
                dealer_hedge_buy = buy
                dealer_hedge_sell = sell
                dealer_hedge_net = net
            elif name == "合計":
                result["total_net"] = net

        # 自營商 = 自行買賣 + 避險
        result["dealer_buy"] = dealer_self_buy + dealer_hedge_buy
        result["dealer_sell"] = dealer_self_sell + dealer_hedge_sell
        result["dealer_net"] = dealer_self_net + dealer_hedge_net

        logger.info(
            "BFI82U parsed: foreign_net=%.0f, trust_net=%.0f, dealer_net=%.0f",
            result.get("foreign_net", 0),
            result.get("trust_net", 0),
            result.get("dealer_net", 0),
        )
        return result

    def save(self, date: str, data: dict) -> None:
        """存入 raw_institutional，用 INSERT OR REPLACE。"""
        with get_connection(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO raw_institutional
                   (date, foreign_buy, foreign_sell, foreign_net,
                    trust_buy, trust_sell, trust_net,
                    dealer_buy, dealer_sell, dealer_net,
                    total_net, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date,
                    data["foreign_buy"],
                    data["foreign_sell"],
                    data["foreign_net"],
                    data["trust_buy"],
                    data["trust_sell"],
                    data["trust_net"],
                    data["dealer_buy"],
                    data["dealer_sell"],
                    data["dealer_net"],
                    data["total_net"],
                    datetime.now().isoformat(),
                ),
            )
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/test_collectors/test_twse.py -v
```

Expected: All 7 tests PASS

- [ ] **Step 7: Run full test suite**

```bash
pytest tests/ -v
```

Expected: All 13 tests PASS (6 schema + 7 TWSE)

- [ ] **Step 8: Commit**

```bash
git add collectors/twse.py tests/fixtures/twse/ tests/test_collectors/
git commit -m "feat: add TWSE institutional investors collector with full test coverage"
```

---

## Task 8: CLI entry point

**Files:**
- Create: `main.py`

- [ ] **Step 1: Write main.py**

```python
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
```

- [ ] **Step 2: Smoke-test CLI**

```bash
python main.py init-db
```

Expected output:
```
Database initialized: data/premarket.db
  broker_tags: 6 entries imported
  watchlist:   2 entries imported
```

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add CLI with init-db and collect institutional commands"
```

---

## Task 9: End-to-end verification

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -v
```

Expected: All tests PASS

- [ ] **Step 2: Test init-db from scratch**

```bash
rm -f data/premarket.db
python main.py init-db
```

Verify output shows correct counts.

- [ ] **Step 3: Test collect with a real date**

```bash
python main.py collect institutional --date 2026-04-08
```

If the date is in the future or returns no data, this will show "no data" — that's expected. The important thing is no crash.

- [ ] **Step 4: Verify DB contents (if data was collected)**

```bash
sqlite3 data/premarket.db "SELECT date, foreign_net, total_net FROM raw_institutional;"
```

- [ ] **Step 5: Final commit (if any cleanup needed)**

```bash
git add -A
git commit -m "chore: Round 1 complete — project skeleton + TWSE institutional collector"
```
