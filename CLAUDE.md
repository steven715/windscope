# CLAUDE.md — 開發規範

## 專案概述

台股開盤前情報收集系統（Pre-Market Intelligence System）Phase 1 MVP。
自動收集匯率、期貨、籌碼三個維度的原始資料，計算衍生指標，存入 SQLite。

Phase 1 只做 Collection（Layer 1）和 Integration（Layer 2）。
**不做訊號判斷、不做分析引擎、不做 UI。**

---

## 技術棧

- Python 3.10+
- SQLite（`sqlite3` 標準庫，不用 ORM）
- `requests` + `beautifulsoup4`
- `argparse` 或 `click`（CLI）
- **不要用 pandas**——資料量極小，用原生 Python 處理

---

## 目錄結構

```
premarket/
├── CLAUDE.md
├── main.py                   # CLI 進入點
├── requirements.txt
├── config/
│   ├── settings.py           # 可調參數集中管理
│   ├── watchlist.json
│   └── broker_tags.json
├── collectors/               # Layer 1
│   ├── base.py               # BaseCollector ABC
│   ├── twse.py
│   ├── taifex.py
│   ├── fx.py
│   └── chip.py
├── integration/              # Layer 2
│   ├── fx_metrics.py
│   ├── futures_metrics.py
│   └── chip_metrics.py
├── db/
│   ├── schema.py
│   └── connection.py
├── jobs/
│   ├── after_close.py
│   ├── after_night.py
│   ├── before_open.py
│   └── backfill.py
├── utils/
│   ├── trading_calendar.py
│   ├── http_client.py        # 統一的 HTTP 封裝（retry、delay、UA）
│   ├── notify.py
│   └── logger.py
├── tests/
│   ├── conftest.py           # 共用 fixtures
│   ├── fixtures/             # HTML snapshots、JSON samples
│   │   ├── twse/
│   │   ├── taifex/
│   │   └── fx/
│   ├── test_collectors/
│   │   ├── test_twse.py
│   │   ├── test_taifex.py
│   │   ├── test_fx.py
│   │   └── test_chip.py
│   ├── test_integration/
│   │   ├── test_fx_metrics.py
│   │   ├── test_futures_metrics.py
│   │   └── test_chip_metrics.py
│   ├── test_db/
│   │   └── test_schema.py
│   └── test_jobs/
│       └── test_job_flow.py
├── docs/
│   └── data_sources.md       # 資料來源文件
├── data/                     # .gitignore
│   └── premarket.db
└── logs/                     # .gitignore
```

---

## 編碼規範

### 命名

- 檔案、函式、變數：`snake_case`
- 類別：`PascalCase`
- 常數：`UPPER_SNAKE_CASE`（集中在 `config/settings.py`）
- 資料庫欄位：`snake_case`，與 Python 變數名一致

### 型別提示

所有公開函式必須有 type hints，包含回傳值：

```python
def compute_fx_metrics(date: str, db_path: str) -> dict | None:
    ...
```

### Docstrings

所有公開函式和類別寫 docstring，說明：做什麼、參數意義、回傳什麼。不用寫小說，三行以內：

```python
def get_previous_trading_day(date: str) -> str:
    """回傳 date 的前一個交易日（YYYY-MM-DD）。跳過週末和國定假日。"""
```

### 錯誤處理

- Collector 內部 catch exception，記 log，回傳 `None` 或 `False`
- 一個 collector 失敗不能讓整個 job 掛掉
- **不要用 bare `except:`**，至少 `except Exception as e:`
- 所有 HTTP 錯誤記錄 URL + status code + response body 前 200 字

### Logging

- 用 `logging` 標準庫，不要 `print()`
- Logger name 用模組路徑：`logging.getLogger(__name__)`
- 層級：正常流程 `INFO`，資料缺失 `WARNING`，失敗 `ERROR`
- 每個 collector 的 `run()` 開始和結束各一行 log

### Import 順序

標準庫 → 第三方 → 本地模組，各區之間空一行。

---

## 測試規範

### 原則

1. **Collector 測試不打真實 HTTP**——用本地 fixture 檔案模擬回應
2. **Integration 測試不依賴真實資料**——在 in-memory SQLite 中塞假資料
3. **每個模組至少一個 happy path + 一個 failure path 測試**
4. **測試可以獨立跑**——不依賴外部網路、不依賴 data/ 下的 db 檔案

### 測試框架

用 `pytest`。放在 `requirements-dev.txt` 或 `requirements.txt` 的 dev section。

### Fixture 策略

#### Collector fixtures（`tests/fixtures/`）

每個資料來源，把真實回應存成 `.html` 或 `.json` 檔案：

```
tests/fixtures/twse/
├── bfi82u_20260408.json      # 三大法人買賣超
├── fmtqik_20260408.json      # 每月行情（加權指數）
├── t86_20260408.json         # 外資個股買賣超
└── stock_day_2330_20260408.json
```

**如何取得 fixture：** 第一次實作 collector 時，手動 curl 或瀏覽器抓一份真實回應存檔。在 fixture 檔案開頭加註取得日期和 URL。

#### Collector 測試寫法

用 `monkeypatch` 或 `unittest.mock.patch` 替換 HTTP 層：

```python
# tests/test_collectors/test_twse.py

def test_parse_institutional_investors(tmp_path):
    """測試三大法人買賣超 JSON 的解析邏輯。"""
    fixture = Path("tests/fixtures/twse/bfi82u_20260408.json").read_text()
    
    with patch("collectors.twse.http_get") as mock_get:
        mock_get.return_value = json.loads(fixture)
        
        collector = TWSECollector(db_path=str(tmp_path / "test.db"))
        data = collector.collect_institutional("2026-04-08")
    
    assert data is not None
    assert "foreign_net_buy" in data
    # 驗證解析出的數字與 fixture 中的原始值一致


def test_parse_institutional_investors_empty_response(tmp_path):
    """測試假日或無資料時的處理。"""
    with patch("collectors.twse.http_get") as mock_get:
        mock_get.return_value = {"stat": "很抱歉，沒有符合條件的資料"}
        
        collector = TWSECollector(db_path=str(tmp_path / "test.db"))
        data = collector.collect_institutional("2026-04-06")  # 假日
    
    assert data is None
```

#### Integration 測試寫法

用 in-memory SQLite + 手動塞入測試資料：

```python
# tests/test_integration/test_fx_metrics.py

@pytest.fixture
def db_with_fx_data():
    """建立 in-memory DB 並塞入測試用的 raw_fx 資料。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    
    # 前一交易日的 close_16
    conn.execute("""
        INSERT INTO raw_fx (date, currency_pair, close_16, quote_0845)
        VALUES ('2026-04-08', 'USD/TWD', 31.50, NULL)
    """)
    # 當日的 quote_0845
    conn.execute("""
        INSERT INTO raw_fx (date, currency_pair, close_16, quote_0845)
        VALUES ('2026-04-09', 'USD/TWD', NULL, 31.35)
    """)
    conn.commit()
    return conn


def test_fx_delta_twd_bullish(db_with_fx_data):
    """台幣升值 > 0.1 應判定為 bullish。"""
    result = compute_fx_metrics("2026-04-09", conn=db_with_fx_data)
    
    assert result["fx_delta_twd"] == pytest.approx(-0.15)
    assert result["fx_direction"] == "bullish"


def test_fx_delta_twd_neutral(db_with_fx_data):
    """台幣變動 < 0.1 應判定為 neutral。"""
    # 覆寫 quote_0845 讓 delta 很小
    db_with_fx_data.execute("""
        UPDATE raw_fx SET quote_0845 = 31.48 
        WHERE date = '2026-04-09' AND currency_pair = 'USD/TWD'
    """)
    
    result = compute_fx_metrics("2026-04-09", conn=db_with_fx_data)
    
    assert result["fx_direction"] == "neutral"
```

### DB 測試

```python
def test_schema_creates_all_tables():
    """init-db 應建立所有必要的表。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {t[0] for t in tables}
    
    expected = {"raw_fx", "raw_futures", "raw_chip", "broker_tags",
                "watchlist", "daily_metrics", "daily_stock_metrics"}
    assert expected.issubset(table_names)


def test_idempotent_insert():
    """同一天重複寫入不應報錯，且資料為最新值。"""
    conn = sqlite3.connect(":memory:")
    create_all_tables(conn)
    
    conn.execute("""
        INSERT OR REPLACE INTO raw_futures (date, night_close)
        VALUES ('2026-04-08', 20000)
    """)
    conn.execute("""
        INSERT OR REPLACE INTO raw_futures (date, night_close)
        VALUES ('2026-04-08', 20050)
    """)
    
    row = conn.execute(
        "SELECT night_close FROM raw_futures WHERE date = '2026-04-08'"
    ).fetchone()
    assert row[0] == 20050
```

### Job 流程測試

```python
def test_after_close_continues_on_collector_failure(tmp_path):
    """單一 collector 失敗不應中斷整個 job。"""
    # Mock 所有 collector，讓其中一個 raise exception
    with patch("collectors.twse.TWSECollector.run", return_value=True), \
         patch("collectors.taifex.TAIFEXCollector.run", side_effect=Exception("模擬失敗")), \
         patch("collectors.chip.ChipCollector.run", return_value=True):
        
        result = run_after_close("2026-04-08", db_path=str(tmp_path / "test.db"))
    
    assert result["twse"] == True
    assert result["taifex"] == False
    assert result["chip"] == True
```

### 測試涵蓋重點清單

| 模組 | 必須測試 |
|------|----------|
| `db/schema.py` | 建表成功、冪等 INSERT |
| `collectors/twse.py` | 每個資料端點的 parse 邏輯（正常 + 無資料 + 格式異常） |
| `collectors/taifex.py` | HTML 表格 parse（正常 + 無資料） |
| `collectors/fx.py` | 各來源 parse（正常 + 無資料 + 來源切換 fallback） |
| `collectors/chip.py` | parse 邏輯（或 CSV import 邏輯） |
| `integration/fx_metrics.py` | delta 計算、方向分類、亞幣同步判斷 |
| `integration/futures_metrics.py` | spread 計算、除息調整、均量比 |
| `integration/chip_metrics.py` | 金額計算、連續天數、MA20、price_zone |
| `utils/trading_calendar.py` | 週末排除、假日排除、前一交易日 |
| `jobs/*` | collector 失敗時的 graceful degradation |

---

## HTTP 封裝（`utils/http_client.py`）

所有 collector 必須透過統一的 HTTP client 發請求，不要各自寫 `requests.get()`：

```python
def http_get(url: str, params: dict = None, 
             headers: dict = None, timeout: int = None) -> requests.Response:
    """
    統一的 GET 請求，內建：
    - User-Agent header
    - timeout（預設 settings.HTTP_TIMEOUT）
    - 指數退避 retry（預設 settings.HTTP_RETRIES 次）
    - 每次請求前 random sleep（settings.HTTP_DELAY_MIN ~ MAX）
    - 失敗時 log URL + status code
    """
```

這樣做的好處：
1. 測試時只需 mock `http_client.http_get` 一個入口
2. 禮貌延遲和 retry 邏輯集中管理
3. 未來如果要加 proxy 或 rate limit 只改一處

---

## DB 連線管理（`db/connection.py`）

```python
from contextlib import contextmanager

@contextmanager
def get_connection(db_path: str = None):
    """
    Context manager，自動 commit/rollback/close。
    所有 DB 操作都走這裡，不要自己 sqlite3.connect()。
    
    用法：
        with get_connection() as conn:
            conn.execute("INSERT ...")
    """
```

測試時傳入 `":memory:"` 即可使用 in-memory DB。

---

## 資料來源不確定時的處理

如果某個來源你無法確認 URL 或格式：

1. **寫 stub**，讓 `collect()` 回傳 `None`
2. **在函式開頭加 TODO block**：

```python
def collect_night_session(self, date: str) -> dict | None:
    # TODO: 待驗證
    # 來源：期交所盤後資料 https://www.taifex.com.tw/cht/3/futContractsDate
    # 已知問題：需要 POST 查詢，回傳 HTML，夜盤欄位位置未確認
    # 狀態：STUB
    logger.warning("collect_night_session is a stub, returning None")
    return None
```

3. **在 `docs/data_sources.md` 中記錄狀態**

**絕對不要猜測 parser 邏輯。** 寧可留 stub 也不要寫一個看起來能跑但解析錯誤資料的 parser。

---

## Git 規範

- Commit message 中文或英文皆可，但要有意義
- 每完成一個 Round（見實作 prompt）做一次 commit
- `.gitignore` 包含：`data/`, `logs/`, `__pycache__/`, `.pytest_cache/`, `*.pyc`

---

## 開發順序

嚴格按照實作 prompt 中的 Round 1→5 順序。每個 Round 完成後：

1. 跑 `pytest` 確認所有測試通過
2. 手動跑一次相關的 CLI 指令確認可用
3. 再進入下一個 Round
