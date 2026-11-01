# ADR-0005：windscope（資料＋研究層）與 tradewind（交易引擎）的邊界

- **狀態**：Proposed（等 tradewind 上 GitHub、雙 repo session 定稿後改 Accepted）
- **日期**：2026-11-01
- **脈絡背景**：使用者釐清長期目標後，把專案定位成兩層：windscope 是「只要有原始資料就能
  重導一切」的基礎設施＋研究層；tradewind 是接真行情、跑模擬盤、最終自動化交易的引擎
  （參考 LEAN 的 PortfolioTarget 與自建風控）。兩個 repo 要能平行開發，前提是介面先定死。
  本 ADR 記錄邊界、共用方式與介面契約的位置；契約本體在 `docs/contracts/`。

---

## 脈絡（Context）

- windscope 已有的資產：原始資料管線（含 `collected_at` 出處）、交易日曆與夜盤歸屬語意、
  `backfill`／`recompute` 的冪等重算、規則版本＋驗證記分、還原股價、回測、判斷有效性統計。
- windscope 的結構債：K 線散在 `raw_index`／`raw_stock_daily`／`raw_futures` 三張長相不同的表；
  沒有商品主檔；`collected_at` 存在但沒被當成「時點正確性」的規則使用。
- 回測天生只有歷史；要驗「實時場景下策略與回測一致」必須有另一個會接行情、有帳本狀態的東西。
- 散戶量化最常見的死法：回測與線上各寫一份特徵計算，live 對不上回測、永遠查不出原因。
- 資料量極小（每日數十列、少數商品），ADR-0001 的 stdlib-first 在兩邊都成立。

## 決策（Decision）

### 1. 職責切分

| | windscope | tradewind |
|---|---|---|
| 擁有 | 原始資料（bars）、參考資料（instruments、corporate_actions、交易日曆）、衍生指標、回測、驗證與統計、**targets 的產出** | 行情源 adapter、帳本（部位／現金／成交）、PortfolioTarget 的落實（目標→訂單）、風控、模擬成交、之後的真下單 |
| 時間性 | 歷史、離線、可重算 | 實時、線上、有狀態 |
| 真相來源 | `f(raw, config)` | 帳本 |
| 不做 | 下單、帳本、盤中常駐（`/live` 唯讀除外） | 重算特徵、自己抓歷史資料、自己定義還原規則 |

### 2. 介面只有一份，放 `docs/contracts/`

四張表的 JSON Schema＋fixture＋語意規則（時區、時點正確性、原始價、目標狀態非訂單、衍生資料不過契約）。
**兩個 repo 的 contract test 讀同一份 fixture**；改介面＝先改契約、兩邊測試都綠。傳輸方式 v0 刻意最笨：
SQLite 表＋`GET /api/targets?since=`／JSONL 檔，不用 message queue。

### 3. 策略與特徵是「共用純函式庫」，不是「資料交換」

tradewind 需要在每根新 bar 上算的任何東西（趨勢三態、乖離、之後的任何特徵），都 import
與 windscope 回測**同一份 code**（`f(bars, params) -> signal`，不碰 DB、不碰網路）。windscope 現有的
`trend_signal.evaluate_series` 已是這個形狀，是第一個要抽出來的。tradewind **不得**自己重寫特徵。

### 4. targets 的語意

- 是「希望的最終部位」（占 NAV 的權重，正多負空零空手），不是訂單；冪等（同 key 重送同值）。
- tradewind 負責把現況調到目標；風控**只能縮小或歸零，不能翻向**。
- 每筆帶 `strategy_id`／`rule_version`／`inputs_ref`，績效按版本分開記、可回 windscope 重導。
- `valid_until` 到期只停止新進，平倉由策略明確送 `weight=0`（符合方法論「出場綁大勢轉向、無固定 % 止損」）。

### 5. repo 形狀：維持兩個 repo，但承認代價

兩個 repo 各自生命週期（研究層 vs 常駐引擎）是成立的理由。代價是共用純函式庫的版本管理：
初期以 git tag 釘版、tradewind 以 `pip install git+…@tag` 取用。**若共用庫變動頻率高到每週要發
多次 tag，改成 monorepo（uv workspace：`packages/core`、`apps/windscope`、`apps/tradewind`）**——拆比合容易，
合回去的成本可接受。

## 理由（Rationale）

1. **同一份 code 跑歷史與實時，是「回測可信」的唯一保證。** 資料交換做不到這點，只有共用程式碼做得到。
2. **原始資料＋時點正確性是基礎設施的核心資產**；衍生的全是 cache。契約只傳原始與參考資料，衍生留在各自的 cache 裡，兩邊才不會為了同步 cache 互相綁死。
3. **targets 當目標狀態而非訂單**，讓策略層不必知道成交細節（整張／零股、分批、滑價），風控與執行的演化不回頭改策略。
4. **最笨的傳輸**在此資料量下零成本，且讓兩邊可以用檔案離線測試。
5. **契約放 windscope repo 的 `docs/contracts/`** 而非第三個 repo：現階段 windscope 是唯一的生產者；等有第二個生產者再搬。

## 承擔的代價（Consequences）

- windscope 要做一次 K 線統一（`bars`＋`instruments` view → 搬遷）與 `available_at` 語意落地；回測改成只讀 `available_at ≤ t`。
- 策略庫抽出後，windscope 內部 import 路徑會變；回測與 `/stats` 必須證明結果與抽出前一致（golden test）。
- 兩 repo 的 contract test 要同步維護；契約 fixture 一改，兩邊都要跑。
- 模擬盤、券商 API、常駐程序只能在本機（憑證、網路、長時間執行），cloud session 只做純邏輯與測試。
- **守則**：任何「tradewind 需要某個 windscope 內部表」的需求，一律回到這裡重新審視——不是加一條資料流，就是把它變成純函式庫的一部分。

## 替代方案（Alternatives considered）

- **tradewind 自己重算特徵、只拿 bars**：兩份實作必分岔。否決。
- **windscope 直接輸出訂單**：把成交細節塞進研究層，風控與執行無法獨立演化。否決。
- **Kafka／Redis Streams 等隊列**：資料量不需要，增加常駐元件與除錯面。否決；需要推播時加 webhook。
- **移植 LEAN**：工程量不成比例。只借 Alpha → PortfolioConstruction → Risk → Execution 四段與 PortfolioTarget 概念。
- **一開始就 monorepo**：對單人開發其實摩擦最低；因使用者已有兩個 repo 與既有本地討論，先維持兩個，設定明確的回頭條件（上文 §5）。

## 何時重新考慮（Revisit triggers）

- 共用策略庫一週內需要發 ≥2 次 tag → 改 monorepo。
- 出現第二個 targets 生產者（例如另一套研究 repo）→ 契約搬到獨立 repo。
- 要接盤中 tick／1 分 K 且商品數成長 → 重新評估 `bars` 的儲存（仍先試 SQLite 分表，再談其他）。
- 模擬盤連續一段期間的成交與回測重放誤差超過可接受範圍 → 先查共用庫是否被繞過，再查成交模型。
