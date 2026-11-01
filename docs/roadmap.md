# Roadmap / TODO

專案後續重點與待辦。硬規則見 `CLAUDE.md`，方法論見 `docs/logic_*.md`。

---

## 🎯 下一個重點：回測層（Layer 5，backtest）

**目標**：拿系統現有的**兩套訊號當「動作(action)」**，回測其歷史表現，
用來**提煉/驗證交易策略與模型**（而非只看單日命中率）。

兩套訊號來源：
1. **大盤市場訊號** `signals`（偏多/偏空/中性 + 信心）——來源〈開盤前三件事〉。
2. **個股趨勢訊號** `stock_trend_signals`（UP/DOWN/FLAT + action_hint 關注建倉區/觀望/…
   + position_hint 加減倉）——來源趨勢四步（`docs/logic_trend.md`）。

**思路（待細化）**：
- 把 action_hint / position_hint 對應成**部位動作**（進場/加碼/減碼/出場/迴避）。
- 用**還原股價**（已建 `integration/price_adjust.py`）算持有報酬、最大回撤、勝率、
  盈虧比、期望值；出場綁「大勢轉向 / gate 轉壞」（無固定 % 止損，符合方法論）。
- 逐版本（`TREND_RULE_VERSION` / `SIGNAL_RULE_VERSION`）比較，**回測驅動門檻調整**——
  呼應「先假設原文為真、累積數據驗證可靠性」。
- 資料多為歷史計算，可離線重跑（純函數 `f(raw, config)`）。

**前置（多半已備）**：
- ✅ 個股還原日K（`raw_stock_daily` + 還原）＝報酬計算的價格基礎。
- ✅ 趨勢訊號有 action/position 語意。
- 🔲 需要：把訊號序列 → 部位序列 → 績效指標的回測引擎 + 頁面呈現。

> 這是使用者指定的下一個主線項目。動工前先給計畫。

---

## 其他待辦（次要）

- **還原股價升級**：目前用「±10% 漲跌幅限制偵測公司行為缺口 + 免費配息比例」自還原；
  若日後有合法可自動化的**還原股價**來源（FinMind Sponsor 等），可替換更精準（見
  `docs/data_sources.md`、`integration/price_adjust.py`）。
- **基本面資料源（顯微鏡/第二步）**：`fundamental_gate` 目前恆 UNKNOWN。接月營收/財報後
  才能真正放行「關注建倉區」。
- **主力分點歷史**：分點明細無免費自動來源（需 FinMind Sponsor 或手動 CSV `分點匯入`）；
  外資動向與股價位置已可免費回補，分點維度暫維持手動。
- **對外認證**：ngrok 對外目前無認證、且有會改資料的 POST 端點（見 auto-memory）。

---

## 跨專案：與 tradewind 的邊界與契約

- 邊界決策：`docs/adr/0005-windscope-tradewind-boundary.md`（windscope＝資料＋研究層、tradewind＝交易引擎；
  策略是共用純函式庫、不是資料交換；targets 是目標狀態不是訂單）。
- 契約草案：`docs/contracts/`（bars／instruments／corporate_actions／targets 的 schema＋fixture＋語意規則），
  `tests/test_docs/test_contracts.py` 鎖 fixture；tradewind 端應有對稱測試讀同一份 fixture。
- windscope 端落地順序：① ✅ `bars_v` 映射三張 raw 表＋`instruments` 表（`db/bars.py`、`db/instruments.py`、`/api/bars`、`/api/instruments`）
  → ② `published_at/ingested_at` 已在契約與 view；回測改讀 `published_at ≤ t` **未做** → ③ 抽 `trend_signal` 為共用純函式庫（golden test）**未做**
  → ④ ✅ `targets` 表＋`/api/targets`＋`POST /api/targets/{id}/lock`＋`integration/targets_builder`（掛 before_open）
  → ⑤ ✅ events／account_snapshots 入庫（`db/events.py`、`POST/GET /api/events`、after_close 掃 inbox）→ ⑥ 即時串流（WS）與 1m 落地：需券商行情源，**本機做**。
- 待定事項列在 `docs/contracts/README.md` 末段，等 tradewind 上 GitHub 後在雙 repo session 定稿。

---

## UI 分離（ADR-0006）

- 後端只出 JSON API（FastAPI＋OpenAPI）；UI 在獨立前端 repo（Vite＋TypeScript＋React），同一個前端吃 windscope／tradewind／錢包三個 API。
- 本 repo 不再新增 Jinja 頁；既有頁面過渡期保留，**API-first**：先補每頁對應的 `/api/*`（`/stats` 已有 `/api/call-stats`），前端做到哪頁、哪頁的 Jinja 才下線。
- 前端第一批只做兩頁：判斷有效性（windscope）、狀態頁＋kill switch（tradewind，按鈕走 ops）。
