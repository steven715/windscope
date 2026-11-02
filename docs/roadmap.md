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
- ✅ 個股趨勢訊號回測引擎（`integration/backtest.py`，`/backtest`）。
- ✅ **盤前判斷有效性統計**（`integration/call_stats.py`，`/stats`、`/api/call-stats`）——見下節。
- 🔲 大盤盤前判斷接進多日持有／跟大勢出場的回測（目前只做單日 open→close / close→close）。

> 這是使用者指定的下一個主線項目。動工前先給計畫。

### 盤前判斷有效性（Layer 4b，`/stats`）

**為什麼先做這個**：規則來源是文章經驗值（作者自承「沒科學根據」），合成與信心加減分是代碼自創；
在加任何新資料源／新規則之前，先用**現有資料**回答「這套到底有沒有 edge」（見 ADR-0004）。
純離線、唯讀，不改門檻、不寫 DB、不動排程。

四張表各回答一個問題，怎麼讀：

| 表 | 回答 | 讀法 |
|---|---|---|
| ① 基準線 | 命中率有沒有贏過傻策略？ | 系統 95% 區間**下界**高於最強基準 → 有 edge；平均高於但區間蓋住 → 可能有、樣本不夠；沒高於 → 看不出 edge。「與結果無關」是保留系統預測分布的獨立期望，是最誠實的隨機基準。 |
| ② 信心校準 | 信心 1–5 有資訊量嗎？ | 命中率應隨信心單調上升；不單調 → 信心加減分邏輯該重審或砍掉。方向拆開看：喊中性落在 ±0.3% 與喊方向真的對，是兩種品質的命中。 |
| ③ 判斷回測 | 照著做會不會賺？ | 偏多做多／偏空做空／中性空手，含成本（0050 口徑 38.5 bps）。看**期望值**與盈虧比，不只勝率；信心加權若贏不過等權 → 信心沒資訊量的第二個證據。`open_to_close` 才是可執行口徑。 |
| ④ 單因子 | 哪個輸入有資訊量、哪個是擺設？ | 各桶漲跌分布差不多 → 該因子沒資訊量。只需 daily_metrics＋指數，回補日也算，樣本比判斷多。 |

**樣本門檻**（`STATS_MIN_SAMPLE=30`／`STATS_MIN_BUCKET=10`）：低於門檻一律標「樣本不足」，不下結論。
每天一筆判斷一年才 ~240 筆，60 筆命中率 60% 的 95% 區間約 ±12pp——分不出跟 50% 有沒有差，
這是「現在覺得沒幫助」的根本原因之一，先累積資料。

**下一步的分工**：系統負責收資料、算指標、產判斷、**記分、回測、算基準**；LLM 負責讀
`/api/call-stats` 這份結構化戰績＋當日盤前輸入，綜合情境給決策建議（JSON 內比例為 0–1、
報酬為小數、`by_confidence` 的 key 經 JSON 序列化後是字串 `"1".."5"`）。LLM 不碰原始資料、
系統不講故事。餵給 LLM 的不該只是「今天偏多信心 3」，而是「同樣條件過去出現 N 次、
命中 k 次、平均報酬 x%、信心 3 歷史命中 y% 與基準無顯著差異」。

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
  → ② ✅ 時點正確性：`tests/test_strategylib/test_pit.py` 證明還原是尺度變換、事後事件不改 ≤t 的趨勢輸出（回測用整段還原序列無前視）→ ③ ✅ `strategylib/`（趨勢三態、還原、Playbook `pullback_hold p1`；golden＋純度測試；ADR-0007）
  → ④ ✅ `targets` 表＋`/api/targets`＋`POST /api/targets/{id}/lock`＋`integration/targets_builder`（掛 before_open）
  → ⑤ ✅ events／account_snapshots 入庫（`db/events.py`、`POST/GET /api/events`、after_close 掃 inbox）
  → ⑥ ✅ Playbook `trend_pullback p1`（個股波段執行：窗口內第一根 1 分 K 收完進場、停損跨日、分批；strategylib 0.2.0）
  → ⑦ `stock_trend_signals` → targets builder（關注建倉區／可加倉／減倉／大勢轉向各發一筆新 target，weight 依批次）→ ⑧ 即時串流（WS）與 1m 落地：需券商行情源，**本機做**。
- 待定事項列在 `docs/contracts/README.md` 末段，等 tradewind 上 GitHub 後在雙 repo session 定稿。

---

## UI 分離（ADR-0006）

- 後端只出 JSON API（FastAPI＋OpenAPI）；UI 在獨立前端 repo（Vite＋TypeScript＋React），同一個前端吃 windscope／tradewind／錢包三個 API。
- 本 repo 不再新增 Jinja 頁；既有頁面過渡期保留，**API-first**：先補每頁對應的 `/api/*`（`/stats` 已有 `/api/call-stats`），前端做到哪頁、哪頁的 Jinja 才下線。
- 前端第一批只做兩頁：判斷有效性（windscope）、狀態頁＋kill switch（tradewind，按鈕走 ops）。
