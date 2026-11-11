# ADR-0006：後端只出 JSON API，UI 獨立成前端 repo

- **狀態**：Accepted（2026-11-01 Steven 確認）
- **日期**：2026-11-01
- **脈絡背景**：windscope 原本是 FastAPI＋Jinja server-render（CLAUDE.md 硬規則 1 明文「Web 不用前端框架」）。
  tradewind 上線後會有第二套 UI（帳本閱讀器、狀態頁、kill switch），使用者另有「個人資產彙總（錢包）」的構想——
  它天生是跨三個後端的畫面。三套各自 server-render 的 UI 對一個人來說是三倍的設計、認證與部署成本；
  使用者明確要求前後端分離。本 ADR 改寫硬規則 1 的 UI 部分。

---

## 決策（Decision）

1. **後端只出 JSON API**：windscope、tradewind（以及未來的錢包）各自是 FastAPI 服務，只提供 `/api/*`＋OpenAPI schema；
   不再新增任何 server-render 頁面。
2. **一個前端 repo**（Vite＋TypeScript＋React）吃三個後端的 API；用 OpenAPI 產生各後端的 client，型別就是契約的延伸。
   三個區域：情報（windscope）、交易（tradewind）、資產（錢包）。
3. **過渡**：windscope 既有 Jinja 頁保留，API-first——先補每頁對應的 `/api/*`，前端做到哪頁、哪頁的 Jinja 才下線。
   tradewind 尚未寫程式，`server/` 直接定義為 API-only。
4. **寫入路徑不變**：前端對 tradewind 的任何「動作」（kill switch、全部平倉、override）只能打 ops 的指令 API，
   由 ops 轉成事件進引擎佇列；前端永遠不直接碰 risk、execution。windscope 的寫入（鎖定 Trade Plan、觀察名單）維持單一入口。
5. **錢包是獨立專案**，不併入 tradewind：它是跨券商／銀行的**唯讀彙總**，變動慢、憑證種類多；tradewind 的 accounting
   是單一交易帳戶、對帳不符即 HALT，兩者可靠性要求不同。錢包讀 tradewind 的 `account_snapshots`（契約已占位）、
   windscope 的 bars 價格，以及其他來源的唯讀 API；等 tradewind 到券商模擬階段有真快照再開。

## 理由（Rationale）

1. 跨後端的畫面（資產彙總、同一天的 Insight＋成交＋事件）只有在前端統一才自然；server-render 要做會變成某個後端去拉別人的資料。
2. 一套設計系統、一套認證、一套部署，單人維護成本最低。
3. OpenAPI 讓契約延伸到前端型別，減少三個後端各自解釋同一個欄位的機會。
4. 錢包獨立是風險隔離：任何一家銀行 API 改版不應該成為交易系統的事故。

## 承擔的代價（Consequences）

- 多一個 repo、多一套 build（Vite）；部署多一個靜態站或容器。
- 過渡期 windscope 同時有 Jinja 與 API 兩套輸出，要靠「不再新增 Jinja 頁」守住不擴散。
- CLAUDE.md 硬規則 1 的 UI 部分改寫；`server/templates/` 進入只刪不加的狀態。
- 前端 repo 的 CLAUDE.md、GLOSSARY 要沿用兩個後端的詞；前端不定義新的領域詞。

## 下線紀錄（Jinja 頁 → windwatch）

| 頁 | 下線日 | 前端對應 | 舊網址 |
|---|---|---|---|
| `/signals` 訊號與驗證紀錄 | 2026-11-11 | windwatch 「情報」：盤前判斷與收盤驗證（近 N 筆小卡由分版的判斷有效性取代） | 有設 `WINDSCOPE_FRONTEND_URL` → 302 `{前端}/#intel`；沒設 → 410 說明 |
| `/stats` 判斷有效性 | 2026-11-11 | windwatch 「情報」：判斷有效性①–④（只看單一規則版本，刻意不做「全部混算」）；⑤ 交易成績在「交易」 | 同上 |

其他 Jinja 頁（首頁的指標與解讀、排程頁的手動執行與設定、資料、個股、回測、觀察名單、匯入）前端還沒有對應，照舊保留。

## 替代方案（Alternatives considered）

- **各後端各自 server-render**：三套 UI、跨後端畫面做不出來。否決。
- **錢包併入 tradewind**：把唯讀彙總綁進「壞掉會賠錢」的系統。否決。
- **前端放在 windscope repo 子目錄**：把 UI 的生命週期綁在一個後端上，等第二個後端出現就要搬。否決。

## 何時重新考慮（Revisit triggers）

- 前端 repo 的大半程式只服務一個後端 → 考慮拆回該後端旁邊的子目錄。
- 錢包需要寫入（例如自動轉帳）→ 它就不再是唯讀彙總，重新評估邊界。
