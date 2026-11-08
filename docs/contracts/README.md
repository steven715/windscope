# windscope ⇄ tradewind 契約（contract v0.2 草案）

> 狀態：**v0.2 草案**（2026-11-01，依 tradewind `docs/review-2026-11-01.md` 的六項決定修訂）。決策脈絡見 `docs/adr/0005-windscope-tradewind-boundary.md`、tradewind ADR-0008／0009／0010。
> 這個目錄是兩個 repo **唯一共同讀的東西**：schema、fixture、語意規則。任何一邊要改介面，先改這裡、兩邊 contract test 都綠，才算改完。

## 這份契約在講什麼

| 檔案 | 方向 | 一句話 |
|---|---|---|
| `bars.schema.json` | windscope 產、兩邊讀 | 統一的 K 線列：所有商品、所有解析度、**原始價**、帶時點（`available_at`） |
| `instruments.schema.json` | windscope 產、兩邊讀 | 商品主檔：代碼規則、資產類別、合約規格、交易時段 |
| `corporate_actions.schema.json` | windscope 產、兩邊讀 | 公司行為（除權息／分割）：還原股價的唯一來源 |
| `targets.schema.json` | **windscope → tradewind** | 目標部位＝**Insight**（主張：方向/信心/有效期/失效）＋**Trade Plan**（行動約束：怎麼進/多大/何時減/何時認錯/什麼不做）；是最終狀態，不是訂單 |
| `events.schema.json` | **tradewind → windscope** | 反向流：Trade Management 狀態轉換、委託、成交、override、風控、系統狀態、健康；每筆帶 `run_mode`，Paper 與實盤永不混算 |
| `account_snapshots.schema.json` | **tradewind → windscope／錢包** | 券商回報的帳戶快照（權益、保證金、持倉）；tradewind 對帳用，未來的個人資產彙總專案也讀這份 |

`fixtures/*.jsonl` 是每張表的範例列；兩個 repo 的 contract test 都讀同一份 fixture。

## 五條不能破的語意規則

1. **時區固定 `Asia/Taipei`**。所有 `ts`／`*_at` 都是帶 `+08:00` 的 ISO 8601；`session_date` 是 `YYYY-MM-DD`。
2. **時點正確性（point-in-time）**：每一列都有一對時刻——`published_at`＝**可知時刻**（這筆資料最早可被知道：tick＝成交時刻、K 棒＝結束時刻、公告＝公告時刻；來源沒給就依規則推算並註明）與 `ingested_at`＝**取得時刻**（系統實際拿到）。任何在決策時刻 `t` 做的計算，只能用 `published_at ≤ t` 的列；`ingested_at` 只用於診斷與 live/backtest 差異分析。與 tradewind CLAUDE.md 規則 1 的 `publish_at` 同義。
3. **bars 只放原始價**。還原（除權息／分割）是 `f(bars, corporate_actions)` 的純函式輸出，由共用策略庫算，不落在契約裡。這樣同一份 bars 能被不同還原方式重算。
4. **targets 是目標狀態，不是訂單**。同一組 `(strategy_id, instrument_id, valid_from, rule_version)` 重送必須得到同一個 target（冪等；`target_id` 就是這四欄的雜湊）；tradewind 負責把「現在部位」調到「目標部位」，怎麼下單、分幾筆是 tradewind 的事。**Insight 與 Trade Plan 分開**：Insight 可對可錯、與帳戶無關；Trade Plan 由 **Playbook（共用純函式庫裡的程式）** 填出、契約只傳 `playbook_id/version`＋`params`，文字欄位只供人讀。**期貨口數由 `trade_plan.risk` 反推**，`target_weight` 只表方向與相對強度；現股才當 NAV 權重。盤前在 windscope **鎖定**（`POST /api/targets/{target_id}/lock`（帶 `Authorization: Bearer <WINDSCOPE_API_TOKEN>`，ADR-0008）或 CLI `targets lock`，寫 `locked_at`，期限 08:55），沒鎖定＝中性。v0 **一商品同時一策略、一天只進一次**（`max_entries_per_day=1`）。Trade Management 只能做 Trade Plan 列出的動作、停損只能收緊；Risk Management **只能縮小或歸零，不能翻向**（按帳戶淨部位算）。
5. **衍生資料不過契約**。`daily_metrics`、趨勢訊號的中間值、信心加減分細節都留在 windscope；tradewind 要用到的特徵，透過共用純函式庫 import 同一份 code 重算，不透過資料交換。

## 代碼規則（`instrument_id`）

格式 `{market}:{symbol}`，大寫：

| 範例 | 意思 |
|---|---|
| `TWSE:2330` | 上市個股 |
| `TPEX:6488` | 上櫃個股 |
| `TWSE:0050` | 上市 ETF |
| `TWSE:TAIEX` | 加權指數（不可交易，`tradable=false`） |
| `TAIFEX:TX` | 台指期**連續**（換月規則在 instruments） |
| `TAIFEX:TXF202606` | 台指期特定月份合約 |
| `FX:USDTWD` | 匯率（不可交易，參考用） |

## 串流與回放（bars）

- 串流推兩種東西：**逐筆 tick**（`resolution=tick`）與**已收完的 1 分 K**（在 `end` 時刻發出，`available_at ≥ ts + 1m`）。
  **絕不推未收完的 K 棒**：否則回放只剩最後一版，做不到逐筆相同。較粗週期（5m、15m…）由消費端自行合成。
- 歷史回放用同一份 schema、同一個順序（先 tick 後該分鐘的 1m bar）；tradewind 的引擎只看到一條迭代器。
- `invalidation.conditions[].field=last` 指最新 tick 的成交價；只有 1m bar 可用時退回最近收完那根的 close。
- 心跳：串流**閒置** N 秒（起始 5）就送一筆 `{"heartbeat": ts}`（有資料在流時不另送）；消費端的 data-age 以「最近一筆訊息（資料或心跳）」算。
- K 棒的 `published_at` 一律＝結束時刻（可知時刻）；若該分鐘之後一段時間沒成交，這根 K 會在時鐘走到結束時刻時由串流端收掉發出
  （起始每秒檢查一次），所以實際收到的時間可能晚於 `published_at` 幾秒。
- **端點**：`ws://{host}/ws/bars?instruments=TAIFEX:TMF,…`（不給＝全部）；每筆一個 JSON 物件（契約 bars 列或心跳）。
  來源沒開（`MARKET_SOURCE=none`）只會收到心跳；歷史用 `GET /api/bars?resolution=1m|tick`。

## 保證金

`instruments.initial_margin / maintenance_margin`（TWD/口）由 windscope 依期交所公告更新，`as_of` 為生效日；
tradewind 算口數時取「`trade_plan.risk` 反推的口數」與「可用保證金÷原始保證金×緩衝」的較小者。fixture 的數字為示意。

## 時段與歸屬

- 股票／指數：`regular` 09:00–13:30。日 K 的 `ts` = 09:00:00+08:00。
- 期貨：`regular` 08:45–13:45、`night` 15:00–05:00（次日）。**夜盤歸屬次一交易日**（`session_date` 往後推，週五夜盤歸下週一），與現行 `GLOSSARY.md`「夜盤歸屬」一致。
- 跨日的夜盤 bar：`ts` 用實際開始時刻（例如 `2026-06-11T15:00:00+08:00`），`session_date` = `2026-06-12`。

## 傳輸方式（v0 刻意最笨）

- windscope → tradewind：`targets` 落 SQLite 表＋`GET /api/targets?since=<ts>`（JSON 信封 `{schema_version, records: [...]}`）；bars 歷史 `GET /api/bars`、即時 WS 串流（含心跳）；離線交換用 JSONL 檔。
- tradewind → windscope：每交易日收盤後寫 `events/{run_mode}/{session_date}.jsonl` 與 `account_snapshots/…`（只增不改），windscope 18:30 讀檔入庫；`POST /api/events`（冪等 by `event_id`）留給之後即時回流，v0 只做寫檔。**不用 message queue**。
- 需要推播再加 webhook；契約不變。

## 版本

- `schema_version` 採 `MAJOR.MINOR`。MINOR＝只加可選欄位（向後相容）；MAJOR＝改語意或刪欄位。
- **0.1 → 0.2（2026-11-01，草案期不守相容）**：`available_at` → `published_at`＋`ingested_at`；targets 加必填 `locked_at`、`trade_plan.params`、`trade_plan.max_entries_per_day`；新增 `events`、`account_snapshots`；instruments 加保證金；bars 加 `tick`。
- `targets` 每列自帶 `schema_version`（量小、要可追溯）；`bars` 不帶（量大），版本由 API 信封或檔名（`bars.v0.jsonl`）表達。
- 兩邊各自記錄「支援的 schema_version 範圍」，contract test 用 fixture 鎖住。

## 從現況對過去（windscope 端的遷移路徑）

| 現在 | 契約 | 做法 |
|---|---|---|
| `raw_index(date, o,h,l,c)` | bars `TWSE:TAIEX` `1d` | **已實作** view `bars_v`（`db/bars.py`、`GET /api/bars`）；資料不搬 |
| `raw_stock_daily(date, stock_id, o,h,l,c,v)` | bars `TWSE:{id}` `1d` | 同上；`volume` 單位股 |
| `raw_futures(date, night_close, night_volume, …)` | bars `TAIFEX:TX` `1d` `session=night` | 同上；只有收盤價，`open/high/low` 為 null；夜盤 `ts` 以歸屬日前一日 15:00 近似 |
| `raw_fx` / `intraday_fx` | bars `FX:USDTWD` `1d` / `5m` | — |
| `market_holidays` | （非契約）`GET /api/calendar` | **已實作**（2026-11-08）：區間休市日 `{date, name}`＋`years_covered`（休市表有資料的年份）；tradewind 排程用來算交易日與換月日 |
| `stock_info` | instruments | **已實作** `instruments` 表＋`config/instruments.json` 種子（`db/instruments.py`、`GET /api/instruments`）；`stock_info` 仍供舊頁面用 |
| `stock_dividends(factor)` | corporate_actions | `kind` 先全填 `cash_dividend`，factor 直接搬 |
| `signals`（盤前判斷） | targets `strategy_id=premarket_call` | **已實作**（`integration/targets_builder.py`，掛在 before_open）：偏多/偏空/中性 → `target_weight` ±信心/5／0，`horizon=intraday`，`valid_from` 08:50、`valid_until` 13:25；`invalidation`＝開盤 30 分內跌破/站上前日低/高點；`trade_plan` 取 `settings.PREMARKET_PLAYBOOK` |
| `stock_trend_signals` | targets `strategy_id=trend_signal` | 關注建倉區→進、可加倉→加、減倉→減、大勢離開 UP→0（每次變動＝新 target）；`horizon=swing`；`invalidation`＝跌破價值中樞 N%；`trade_plan` 用 Playbook `trend_pullback p1`（窗口內第一根 1 分 K 收完進場、停損跨日、分批；strategylib 0.2.0）。**已實作** `integration/trend_targets.py`（掛在 after_close）：批數規則與回測同一組 `BACKTEST_*`（3 批均分、加碼冷卻 3 日、超買減碼、閘門假設 PASS），批數有變才發一筆（weight＝累積批次權重、次一交易日 09:00 生效、`valid_until` null）；失效＝收盤跌破月線 2%，停損距離＝收盤到失效價位；閘門 FAIL 不開倉不加碼、只准減／出；`conviction` 不填（趨勢訊號的信心是年線斜率，不是 0–1）；`rule_version`＝`{t2}-{b1}`；狀態（units／last_add_date）記在上一筆 target 的 `inputs_ref.key`，只往前重放（重算某日覆蓋該日 target，不回頭改後面日期） |

映射的權重數字（信心 3 = 多少 weight、建倉一批多大、1R 幾 %）**預設值放 `config/settings.py`**；契約的 `trade_plan` 放的是這一筆實際採用的值，讓 tradewind 不必知道 windscope 的 settings。

## 待定（定稿時要拍板）

- [ ] 期貨連續合約 `TAIFEX:TX`／`TMF` 的換月規則：到期前 N 日切／成交量切？v0 先寫在 instruments 的 `roll_rule`，值由 tradewind 決定。
- [ ] 個人資產彙總（錢包）專案：獨立 repo，讀 `account_snapshots`＋bars 價格＋其他券商／銀行唯讀 API；等 tradewind 到券商模擬階段有真快照再開（tradewind ADR-0010）。
- [x] ~~Playbook 共用純函式庫的位置與發布~~ → `strategylib/`（windscope 子目錄、`pyproject.toml`、tag `strategylib-vX.Y.Z`）；`pullback_hold p1` 已是程式（`strategylib.playbooks`），ADR-0007。
- [x] ~~`target_weight` 的基準~~ → 期貨：方向與相對強度、口數由 `trade_plan.risk` 反推；現股：NAV 權重（已定）。
- [ ] `valid_until` 到期時 tradewind 是「自動平倉」還是「只停止新進」？v0 定 **只停止新進**，平倉由策略明確送 `weight=0`。
- [x] ~~要不要在 targets 帶 `stop_hint`~~ → 已以 `trade_plan.risk.stop_pts`（初始停損，只能收緊）與 `invalidation`（失效≠停損）取代。
- [x] ~~`events` 反向流的 schema~~ → 已起草 `events.schema.json`（kind/action 對照 tradewind 01-objects 的 Fill、OrderEvent、RiskDecision、SystemEvent），等 tradewind 端對稱測試。
- [x] ~~盤前判斷的 `invalidation` 來源~~ → v0：開盤 30 分內現貨跌破（偏多）／站上（偏空）**前一交易日低／高點**（`integration/targets_builder.py`，`settings.PREMARKET_INVALIDATION_UNTIL`）；前日高低無資料則只留文字。
