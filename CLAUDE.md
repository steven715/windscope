# CLAUDE.md — 開發規範

台股開盤前情報系統：常駐 server 每日閉環（收匯率/期貨/籌碼 → 算衍生指標 →
08:50 產訊號判斷[偏多/偏空/中性＋信心＋理由] → 收盤雙基準三分類驗證、累積命中率）
＋ server-rendered Web 畫面。**不做**：下單/交易、盤中即時監控（除 `/live` 驗證觀察）、
歷史回測、ML 模型。專案現況/決策脈絡見 auto-memory（MEMORY.md）與 `docs/`。

---

## ⚠️ 不可違反（IMPORTANT — 修改任一條前先跟我確認）

專案硬約束，違反會破壞正確性或造成不可逆損失：

1. **不用 pandas**；SQLite 只用 stdlib `sqlite3`、不用 ORM；Web 不用前端框架（Jinja server-render）。資料量極小，原生 Python 處理。
2. **所有 DB 操作走 `db/connection.get_connection`**（context manager，自動 commit/rollback/close）；不要自己 `sqlite3.connect()`。測試傳 `":memory:"`。
3. **所有外部請求走 `utils/http_client.http_get/http_post`**（內建 UA/timeout/退避 retry/禮貌延遲）；不要各自 `requests.get()`。
4. **Collector 內 catch exception、記 log、回 `None`/`False`**；一個 collector 失敗不能讓整個 job 掛掉。不用 bare `except:`，至少 `except Exception as e:`。
5. **絕不猜 parser 邏輯**：來源無法確認就留 stub（`collect()` 回 None）＋函式頂 `# TODO 狀態:STUB` block ＋記 `docs/data_sources.md`。寧可 stub 也不要寫一個會解析錯資料的 parser。
6. **可調門檻全集中 `config/settings.py`**；調整任何訊號門檻必須 bump `SIGNAL_RULE_VERSION`（否則新舊規則命中率混在一起統計）。
7. **`/live` 盤中即時驗證唯讀**：不產生新訊號、不寫 DB。
8. **時區 `Asia/Taipei`**：排程時間與「交易日/夜盤歸屬/前一交易日」等日期語意全依此（Docker 以 `TZ` 設定）。
9. **不可逆動作先確認**：不要 force-push `master`、不要覆寫或刪除 `data/premarket.db`（線上資料）。

---

## 部署 / CI-CD（個人單機 docker compose）

- **CI（改完必做，我這邊協助確認）**：`pytest` 全綠 → 較大改動先讓我審「計畫(plan)」或「diff＋測試結果」 → commit。
- **CD（這台機器，讓我看實際畫面）**：`docker compose up -d --build`。
  - Dockerfile 是 `COPY . .` 把 code 烤進 image → **改 Python code 一定要 `--build`**，純 restart 不會更新。
  - bind-mount：`./config`（編輯即時生效、免 build）、`./data`、`./logs`（DB/log 跨重啟保留）。
  - **schema 自癒**：app 啟動時 `create_all_tables` 冪等建表＋PRAGMA-guard 的 ALTER migration，不用手動遷移 DB。
  - 只改 `.env`（token/chat_id）→ `docker compose up -d`（不必 `--build`）。
- 常用 CLI：`python main.py serve | init-db | refresh-holidays | collect | backfill`。
- 要單獨測某個 collector 又不想碰正式資料：`PREMARKET_DB=/tmp/t.db python main.py collect ...`。

---

## 技術棧

Python 3.10+ ・ stdlib `sqlite3` ・ `requests` + `beautifulsoup4` ・
FastAPI + uvicorn + Jinja2 ・ APScheduler（取代 crontab）・ pytest。

## 結構（東西該放哪的意圖）

- `collectors/` — Layer1 原始資料（twse/taifex/fx/chip/holiday），繼承 `base.BaseCollector`
- `integration/` — Layer2 衍生指標（fx/futures/chip_metrics、summary）＋ Layer3 `signal_engine` ＋ Layer4 `verification` ＋ `live_*`
- `server/` — `app.py`（工廠）/`scheduler.py`（排程登錄＋執行紀錄）/`routes/`/`templates/`
- `jobs/` — 各時段 job：`after_night`/`before_open`/`afternoon_fx`/`verify_close`/`after_close`/`refresh_holidays`/`backfill`
- `db/` — `schema.py`（含 `_migrate_columns`）/`connection.py`；`utils/` — `trading_calendar`/`http_client`/`notify`
- `config/` — `settings.py`（所有門檻）/`watchlist.json`/`broker_tags.json`；`tests/` 對應各層；`data/`、`logs/` gitignore

## 編碼

- 命名：`snake_case`、類別 `PascalCase`、常數 `UPPER_SNAKE`（集中 settings.py）；DB 欄位 = Python 變數名。
- 公開函式/類別必須有 type hints（含回傳）與 ≤3 行 docstring（做什麼/參數/回傳）。
- logging 標準庫（不用 `print`），logger name = `logging.getLogger(__name__)`；正常 `INFO`／缺資料 `WARNING`／失敗 `ERROR`；HTTP 錯誤記 URL＋status＋body 前 200 字；每個 collector `run()` 起訖各一行 log。
- import 順序：標準庫 → 第三方 → 本地，各區空一行。

## 測試

- **Collector 不打真實 HTTP**：`patch`/`monkeypatch` 掉 `http_get`，餵 `tests/fixtures/` 的真實回應快照（檔頭註明取得日期＋URL）。
- **Integration 用 in-memory SQLite** 塞假資料，不依賴 `data/` 的 db、不依賴網路。
- 每個模組至少一個 happy path ＋ 一個 failure path；測試可獨立跑。
- 寫法照 `tests/` 既有範例（mock http 層 / `:memory:` conn / `tmp_path` 檔案 DB）。
- 較大改動：跑完 `pytest` 後再用獨立 code-reviewer agent 對抗式審查，**不自審**。

### 測試涵蓋重點清單

| 模組 | 必須測試 |
|------|----------|
| `db/schema.py` | 建表成功、冪等 INSERT、欄位 migration |
| `collectors/*` | 每個端點 parse（正常＋無資料＋格式異常）；fx 含來源路由 |
| `integration/fx_metrics` | delta 計算、方向分類、亞幣同步 |
| `integration/futures_metrics` | spread、除息調整、均量比 |
| `integration/chip_metrics` | 金額、連續天數、MA20、price_zone |
| `utils/trading_calendar` | 週末排除、假日排除、前一/下一交易日 |
| `jobs/*` | collector 失敗時 graceful degradation、休市/週末略過語意 |
| `integration/signal_engine` | 兩票合成、加減分、夾限 1–5、rule_version、個股過濾分類 |
| `integration/verification` | 三分類門檻邊界、雙基準命中、資料缺失、命中率統計 |
| `server/*` | 各 route 回 200＋關鍵內容、API 區間查詢、空狀態、排程紀錄寫入/篩選/刪除 |

## Git

- commit message 中英皆可但要有意義；在 default branch（master）上動手前先開 branch，除非我明說直接進 master。
- commit 只在我要求時做；commit 前 `pytest` 全綠。
- `.gitignore`：`data/`、`logs/`、`__pycache__/`、`.pytest_cache/`、`*.pyc`。

## 工作流程（loop）

1. 非顯而易見的改動：先給計畫、等我放行，再動手。
2. 實作 → `pytest` 全綠 →（較大改動）獨立 reviewer 對抗式審查 → 我審 diff/計畫。
3. 我要求才 commit；要看實際畫面 → `docker compose up -d --build`。
4. 學到的「非顯而易見」事實寫進 auto-memory（MEMORY.md），**不要塞進本檔**——本檔只放會持續適用的硬規則。
