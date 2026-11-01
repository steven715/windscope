# windscope ⇄ tradewind 契約（contract v0.1 草案）

> 狀態：**草案**，等 tradewind 上 GitHub 後在雙 repo session 定稿。決策脈絡見 `docs/adr/0005-windscope-tradewind-boundary.md`。
> 這個目錄是兩個 repo **唯一共同讀的東西**：schema、fixture、語意規則。任何一邊要改介面，先改這裡、兩邊 contract test 都綠，才算改完。

## 這份契約在講什麼

| 檔案 | 方向 | 一句話 |
|---|---|---|
| `bars.schema.json` | windscope 產、兩邊讀 | 統一的 K 線列：所有商品、所有解析度、**原始價**、帶時點（`available_at`） |
| `instruments.schema.json` | windscope 產、兩邊讀 | 商品主檔：代碼規則、資產類別、合約規格、交易時段 |
| `corporate_actions.schema.json` | windscope 產、兩邊讀 | 公司行為（除權息／分割）：還原股價的唯一來源 |
| `targets.schema.json` | **windscope → tradewind** | 目標部位：策略要的「最終狀態」，不是訂單 |

`fixtures/*.jsonl` 是每張表的範例列；兩個 repo 的 contract test 都讀同一份 fixture。

## 五條不能破的語意規則

1. **時區固定 `Asia/Taipei`**。所有 `ts`／`*_at` 都是帶 `+08:00` 的 ISO 8601；`session_date` 是 `YYYY-MM-DD`。
2. **時點正確性（point-in-time）**：每一列都有 `available_at`＝系統**實際拿到它的時刻**。任何在決策時刻 `t` 做的計算，只能用 `available_at ≤ t` 的列。回測與模擬盤都遵守這條，兩邊才對得上。
3. **bars 只放原始價**。還原（除權息／分割）是 `f(bars, corporate_actions)` 的純函式輸出，由共用策略庫算，不落在契約裡。這樣同一份 bars 能被不同還原方式重算。
4. **targets 是目標狀態，不是訂單**。同一組 `(strategy_id, instrument_id, valid_from)` 重送必須得到同一個 target（冪等）；tradewind 負責把「現在部位」調到「目標部位」，怎麼下單、分幾筆、要不要縮水是 tradewind 的事。風控**只能縮小或歸零，不能翻向**。
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

## 時段與歸屬

- 股票／指數：`regular` 09:00–13:30。日 K 的 `ts` = 09:00:00+08:00。
- 期貨：`regular` 08:45–13:45、`night` 15:00–05:00（次日）。**夜盤歸屬次一交易日**（`session_date` 往後推，週五夜盤歸下週一），與現行 `GLOSSARY.md`「夜盤歸屬」一致。
- 跨日的夜盤 bar：`ts` 用實際開始時刻（例如 `2026-06-11T15:00:00+08:00`），`session_date` = `2026-06-12`。

## 傳輸方式（v0 刻意最笨）

- windscope 端：`targets` 落 SQLite 表＋`GET /api/targets?since=<ts>`（JSON 信封 `{schema_version, records: [...]}`）；離線交換用 JSONL 檔。
- tradewind 端：輪詢或讀檔。**不用 message queue**，資料量每天幾十列，隊列是純負擔。
- 需要推播再加 webhook；契約不變。

## 版本

- `schema_version` 採 `MAJOR.MINOR`。MINOR＝只加可選欄位（向後相容）；MAJOR＝改語意或刪欄位。
- `targets` 每列自帶 `schema_version`（量小、要可追溯）；`bars` 不帶（量大），版本由 API 信封或檔名（`bars.v0.jsonl`）表達。
- 兩邊各自記錄「支援的 schema_version 範圍」，contract test 用 fixture 鎖住。

## 從現況對過去（windscope 端的遷移路徑）

| 現在 | 契約 | 做法 |
|---|---|---|
| `raw_index(date, o,h,l,c)` | bars `TWSE:TAIEX` `1d` | 先建 view，再搬 |
| `raw_stock_daily(date, stock_id, o,h,l,c,v)` | bars `TWSE:{id}` `1d` | 同上；`volume` 單位股 |
| `raw_futures(date, night_close, night_volume, …)` | bars `TAIFEX:TX` `1d` `session=night` | 目前只有收盤價，`open/high/low` 可為 null（schema 允許） |
| `raw_fx` / `intraday_fx` | bars `FX:USDTWD` `1d` / `5m` | — |
| `stock_info` | instruments | 補 `asset_class`、`tick_size`、`lot_size` |
| `stock_dividends(factor)` | corporate_actions | `kind` 先全填 `cash_dividend`，factor 直接搬 |
| `signals`（盤前判斷） | targets `strategy_id=premarket_call` | 偏多/偏空/中性 → `target_weight` ±w(信心)/0，`horizon=intraday`，`valid_from` 08:50、`valid_until` 13:30 |
| `stock_trend_signals` | targets `strategy_id=trend_signal` | 關注建倉區→進、可加倉→加、減倉→減、大勢離開 UP→0；`horizon=swing` |

映射的權重數字（信心 3 = 多少 weight、建倉一批多大）**屬於策略參數，放 `config/settings.py`，不寫進契約**。

## 待定（定稿時要拍板）

- [ ] 期貨連續合約 `TAIFEX:TX` 的換月規則：到期前 N 日切／成交量切？v0 先寫在 instruments 的 `roll_rule`，值由 tradewind 決定。
- [ ] `target_weight` 的基準是淨值（NAV）還是可用現金？v0 定 **NAV**。
- [ ] `valid_until` 到期時 tradewind 是「自動平倉」還是「只停止新進」？v0 定 **只停止新進**，平倉由策略明確送 `weight=0`。
- [ ] 要不要在 targets 帶 `stop_hint`（方法論說無固定 % 止損，先不帶）。
