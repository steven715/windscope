# 上線前清單（windscope × tradewind × windwatch，本機）

這份是「第一次在本機把三個 repo 跑起來、接 paper 跑滿一期」照著勾的順序。雲端開發環境連不到台灣資料站、沒有永豐帳戶、
沒有 Caddy，所以標 ⚠ 的都**還沒實際驗證過**：第一次做的結果補回各自的文件（括號裡寫在哪）。

架構：windscope（:8000，docker compose）產訊號／targets／串流；tradewind（paper 常駐＋`serve.py` :8100）照 targets 交易、
把 events 寫進 windscope 收件匣；windwatch（Caddy :8080，同源代理注入兩把 token）給手機看。對外只開 Caddy（ngrok 轉它）。

---

## 0. 機器

- [ ] Docker＋compose（windscope）、Python 3.11+ 與 venv（tradewind）、Node 20+（windwatch build）、Caddy ≥ 2.8、ngrok（要從外面看時）。
- [ ] 時區：windscope 容器用 `TZ=Asia/Taipei`（compose 已設）；tradewind 跑在主機上，排程與「今天」都換算成台北時間，主機時區不影響，
      但 **主機時鐘要準**（NTP）。08:55 鎖定期限、09:00 開盤都看時鐘。

## 1. 金鑰與設定（只放 `.env`／環境變數，不進 git）

| 變數 | 誰用 | 說明 |
|---|---|---|
| `WINDSCOPE_API_TOKEN` | windscope（必填，沒設 compose 起不來）、tradewind（REST＋`/ws/bars`）、Caddy | 同一把；產法：`python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `TRADEWIND_API_TOKEN` | tradewind `serve.py`（ops API）、Caddy | 沒設＝ops API 關閉（前端的 kill switch／全部平倉送不出去） |
| `WINDSCOPE_FRONTEND_URL` | windscope | 前端網址（例 `https://<ngrok 網域>`）；下線的 `/signals`、`/stats` 導過去，沒設回 410 |
| `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | windscope（通知）、tradewind（推播） | 選填；windscope 用 `telegram-setup` skill 設 |
| `TELEGRAM_ALLOWED_USERS` | tradewind `telegram_bot.py` | 只准這些人下 `/halt` 等指令 |
| `FINMIND_TOKEN` | windscope `chip_collect` | 沒設就略過分點（`chip_collect` 預設停用） |
| `MARKET_SOURCE`、`INSTALL_SHIOAJI`、`SHIOAJI_API_KEY`、`SHIOAJI_SECRET`、`SHIOAJI_SIMULATION` | windscope 行情串流 | 先 `replay` 演練，再 `shioaji`（§2.4） |
| `SHIOAJI_API_KEY`、`SHIOAJI_SECRET`（或 `SHIOAJI_SECRET_KEY`） | tradewind `--broker shioaji_sim` | 跟 windscope 同名即可共用一份 `.env`（tradewind 兩個名字都收；兩個都設又不同會拒絕登入） |
| `TRADEWIND_INBOX` | windscope | tradewind `--out` 寫的地方（預設 `data/inbox`，容器內）；兩邊要指同一個實體目錄 |
| `WINDWATCH_USER`、`WINDWATCH_PASS_HASH` | Caddy | 對外那道 HTTP Basic；`caddy hash-password` 產 |

## 2. windscope

1. [ ] `.env` 填好 → `docker compose up -d --build`；`docker compose logs -f` 看到 server 起來、排程登錄。
2. [ ] **全新 DB 跑一次** `docker compose exec premarket python main.py init-db`（灌觀察名單與商品主檔種子）→ `refresh-holidays`。
3. [ ] ⚠ 商品主檔：`config/instruments.json` 種子裡的月份合約是 `TXF202606`／`TMF202606`（2026-06 的）。paper 開始前確認
      `GET /api/instruments` 有**當月與次月**的 TX／TMF（換月由 `strategylib.roll` 算，主檔要有那幾個月）；缺的照
      `docs/contracts/README.md` 補（只走 `db/instruments.py`）。
4. [ ] 健康檢查（帶 `Authorization: Bearer $WINDSCOPE_API_TOKEN`）：`/api/scheduler`（`scheduler_enabled: true`、各 job 有 `next_run`）、
      `/api/calendar?date_from=<今年>-01-01&date_to=<今年>-12-31`（有今年的休市日）、`/api/instruments`。
5. [ ] 收集器單獨試（不碰正式 DB）：`PREMARKET_DB=/tmp/t.db python main.py collect ...`。
6. [ ] 行情串流：先 `MARKET_SOURCE=replay`（演練 DB，不碰正式）確認 `/ws/bars` 有 tick／1m／心跳；再接永豐：
      `INSTALL_SHIOAJI=1`、`MARKET_SOURCE=shioaji`，照 `docs/data_sources.md` 八「本機驗證清單」10 項逐項確認並回填。
7. [ ] 線上 DB `data/premarket.db` **不要刪也不要覆寫**；先備份一份再開始。

## 3. tradewind

1. [ ] `python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev,server]"`（共用庫跟著 pyproject 的 `strategylib-vX.Y.Z` tag）→ `pytest` 全綠。
      之後每次 `git pull` 都重裝再重啟（README「每日 paper 常駐」）。
2. [ ] 契約版本：`contracts/windscope/COMMIT` 對到 windscope 現在的 commit（目前停在 `e2c1bd1`，之後只有 README 改了 lock 的說明）；不對就 `python scripts/sync_contracts.py --commit <sha> --strategylib-tag <tag> --source ../windscope`。
      ⚠ windscope 的 lock API 現在要 `Content-Type: application/json`、過 08:55 回 409（tradewind 不呼叫 lock，只讀 targets）。
3. [ ] 先登記假設：`python scripts/logbook.py --state-dir runtime/ hypothesis add H1 --statement "…" --why "…"`。
4. [ ] 一日回放演練：`python scripts/backtest_day.py --source http://127.0.0.1:8000 --date <最近一個交易日> --out runtime/bt0`。
5. [ ] paper 常駐（期貨、現股各一）：README「每日 paper 常駐」的指令，`--ws ws://127.0.0.1:8000 --out <windscope 收件匣> --state-dir runtime/
      --hypothesis H1`；建議 systemd（`Restart=on-failure`、`EnvironmentFile=.env`）。
6. [ ] API：`TRADEWIND_API_TOKEN=… python scripts/serve.py --state-dir runtime/ --out <收件匣>`（只聽 127.0.0.1:8100）。
7. [ ] 停手演練（**一定要做一次**）：盤後 `python scripts/ops.py --root runtime/ kill-switch --reason "演練"` → 狀態變 HALT →
      先寫一份事故帳 `docs/incidents/<日期>-drill.md`（resume 一定要帶存在的事故帳；標明是演練）→
      `ops.py --root runtime/ resume --reason "演練結束" --incident docs/incidents/<日期>-drill.md`；Telegram `/halt` 也試一次。
      前端的 kill switch 等 §4 好了再試（resume 不在前端）。
8. [ ] 永豐模擬（之後）：`pip install -e ".[shioaji]"`、`--broker shioaji_sim`；**第一次跑之前**照 `docs/domain/13-integrations.md`
      「本機首次驗證清單」23 項，結果補回那份文件。只接決定表上已到 shioaji_sim 的 alpha（`logbook.py decision status`）。

## 4. windwatch（前端）＋Caddy

1. [ ] `npm ci && npm run check` 全綠 → `npm run build` → `dist/` 放到 `/srv/windwatch`。
2. [ ] Caddy：`WINDWATCH_USER`、`WINDWATCH_PASS_HASH`、`WINDSCOPE_API_TOKEN`、`TRADEWIND_API_TOKEN` 設好 →
      `caddy validate --config deploy/Caddyfile` ⚠（從沒實際跑過）→ `caddy run --config deploy/Caddyfile`。
3. [ ] 手動打一遍（`-u user:pass`，`H=http://127.0.0.1:8080`）——預期結果：

| 請求 | 預期 |
|---|---|
| `curl $H/ws-api/api/targets` | 200（JSON） |
| `curl $H/ws-api/watchlist` | 403（不在白名單） |
| `curl -X POST $H/ws-api/api/scheduler/verify_close/run -H 'Sec-Fetch-Site: same-origin' -H 'Content-Type: application/json' -d '{}'` | 202，或 windscope 的 403／409（還沒到排定時間、正在跑） |
| 同上但 `-H 'Content-Type: text/plain'` | 403（Caddy 不放行） |
| 同上但 `-H 'Sec-Fetch-Site: cross-site'` | 403 |
| `curl -X POST $H/ws-api/api/scheduler/before_open/run -H 'Sec-Fetch-Site: same-origin' -H 'Content-Type: application/json' -d '{}'` | 403（windscope：不能從 API 補跑） |
| `curl -X POST $H/tw-api/api/ops/resume -H 'Sec-Fetch-Site: same-origin' -H 'Content-Type: application/json' -d '{}'` | 403（resume 不開放） |
| 不帶 `-u` | 401（Caddy 的 Basic） |

4. [ ] ngrok：`ngrok http 8080`（**不要** `--host-header=rewrite`：同源檢查比 Origin 與 Host）；windscope `.env` 設
      `WINDSCOPE_FRONTEND_URL=https://<ngrok 網域>` → `docker compose up -d`。
5. [ ] 手機打開：今日／情報／交易／系統四區都有資料；舊書籤 `/signals` 導到前端 `#intel`。

## 5. 第一個交易日（照時間看）

| 時刻 | 誰 | 看什麼 |
|---|---|---|
| 05:30（週二～六） | windscope after_night | 系統頁最近一次「完成」；夜盤資料記在下一個交易日 |
| 08:40／08:55 | tradewind paper 啟動（期貨／現股） | 今日頁狀態列帳戶出現、mode NORMAL、資料秒數在跳 |
| 08:50 | windscope before_open | 今日頁有盤前判斷與 targets |
| **08:55 前** | 你 | 今日頁「鎖定」；過了 08:55 沒鎖＝中性（API 也不准晚鎖，要晚鎖只能 CLI，但 tradewind 照樣當中性） |
| 盤中 | tradewind | 今日頁委託／成交每 15 秒更新；斷線會切 REDUCE_ONLY／flatten（看狀態列與時間線） |
| 13:46／13:31 後 | tradewind 收工（期貨／現股，收盤＋寬限） | 收件匣有 `events/paper/<日>.jsonl`、`account_snapshots/paper/<日>.jsonl`；推播一行筆數 |
| 14:30 | windscope verify_close | 情報頁當天「收盤命中」有值 |
| 16:00 | windscope afternoon_fx | 隔天升貶的基準 |
| 18:30 | windscope after_close | 收件匣入庫 → 交易頁有交易成績；系統頁 tradewind 每日紀錄（對帳、狀態切換）有當天的 |

## 6. 跑幾天之後

- [ ] 對帳都是「相符」；有不符會切 HALT，照事故流程寫 `tradewind/docs/incidents/` ＋回歸測試再 resume。
- [ ] 永豐模擬跑幾天後：`python scripts/cash_check.py --out <收件匣> --account sim-fut-01` 看券商欄位跟系統帳現金的差，
      口徑確認後才把現金加進對帳（13-integrations 第 10、20 項）。
- [ ] 滿 20 個交易日：paper 週期自動寫成實驗（擱置）→ 看交易頁的交易成績與實驗帳 → `logbook.py correct experiments <id> verdict 留|殺 --reason …`。
- [ ] 晉級要照決定表（`logbook.py decision gate <alpha> --exp <id>`）；**進實盤要 ADR＋兩步確認**（tradewind 規則 8），不在這份清單裡。

## 已知還沒驗證／還沒做

- Caddy 設定沒實際跑過（§4.2）。
- 永豐：windscope 10 項、tradewind 23 項本機驗證清單都還沒做。
- 現金對帳的口徑（cash_check）。
- 08:55 期限：windscope `TARGET_LOCK_DEADLINE` 是來源（`/api/targets` 帶 `lock_deadline`，前端照它）；tradewind 的 lock_deadline 仍是自己的設定，要改得兩邊一起改。
- 部分 server 測試會打外網（雲端環境被擋、重試拖到 4–5 分鐘）；本機有網路時快很多。
