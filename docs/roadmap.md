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
- ✅ **對外認證**（2026-11-08，ADR-0008）：單一 token `WINDSCOPE_API_TOKEN`（`server/auth.py` middleware）——API／tradewind 用 Bearer、過渡期 Jinja 頁用 HTTP Basic（密碼＝token，不新增登入頁）；Basic 的會改資料請求與 WS 握手要同源；compose 沒設起不來。同源反向代理等前端 repo。

---

## 跨專案：與 tradewind 的邊界與契約

- 邊界決策：`docs/adr/0005-windscope-tradewind-boundary.md`（windscope＝資料＋研究層、tradewind＝交易引擎；
  策略是共用純函式庫、不是資料交換；targets 是目標狀態不是訂單）。
- 契約草案：`docs/contracts/`（bars／instruments／corporate_actions／targets 的 schema＋fixture＋語意規則），
  `tests/test_docs/test_contracts.py` 鎖 fixture；tradewind 端應有對稱測試讀同一份 fixture。
- windscope 端落地順序：① ✅ `bars_v` 映射三張 raw 表＋`instruments` 表（`db/bars.py`、`db/instruments.py`、`/api/bars`、`/api/instruments`）
  → ② ✅ 時點正確性：`tests/test_strategylib/test_pit.py` 證明還原是尺度變換、事後事件不改 ≤t 的趨勢輸出（回測用整段還原序列無前視）→ ③ ✅ `strategylib/`（趨勢三態、還原、Playbook `pullback_hold p1`；golden＋純度測試；ADR-0007）
  → ④ ✅ `targets` 表＋`/api/targets`＋`POST /api/targets/{id}/lock`＋`integration/targets_builder`（掛 before_open）
  → ⑤ ✅ events／account_snapshots 入庫（`db/events.py`、`POST/GET /api/events`、after_close 掃 inbox）；⑤-b ✅ 依成交打分（2026-11-08，`integration/trade_stats.py`、`GET /api/trade-stats`、`/stats` 第⑤段）：一筆交易＝同帳戶同商品從空手到空手的一段部位（含風控平倉、換月、趨勢加減碼被淨額的成交；同日重跑只看最後一次；跨日與日期區間不切半；到期沒成交＝不打分），R 以開倉 target 的 1R 計（＝tradewind 成交 R 的口數加權）；依 run_mode×策略×規則版本×Playbook×引擎版本分組（永不混算）、出場原因（停損／失效／目標／override／flatten 分開）、Insight（開倉方向 vs 當天漲跌）×R、override 有無、帳戶權益曲線（含入出金）；純讀取（tradewind M2 的「windscope 看得到」）
  → ⑥ ✅ Playbook `trend_pullback p1`（個股波段執行：窗口內第一根 1 分 K 收完進場、停損跨日、分批；strategylib 0.2.0）
  → ⑦ ✅ `stock_trend_signals` → targets builder（`integration/trend_targets.py`，掛 after_close；批數規則與回測同一組 `BACKTEST_*`，批數有變才發一筆新 target，weight＝累積批次權重；狀態在上一筆 target 的 `inputs_ref.key`）→ ⑧ 即時串流（WS）與 1m 落地：✅ 雲端能做的部分（2026-11-03）——`integration/market_sessions`（時段歸屬，夜盤歸次一交易日）、
  `integration/bar_builder`（tick → 只在分鐘結束發出的 1m）、`collectors/market_stream`（`StreamRunner`：tick 推 hub、1m 推 hub＋落 `bars`；
  `ReplaySource` 回放；`ShioajiSource` 📌 STUB）、`server/stream.StreamHub`＋`/ws/bars`（tick／1m／心跳）、CLI `stream --jsonl`；
  **本機剩**：`ShioajiSource` 驗證（清單在 `docs/data_sources.md` 八）、`.env` 設 `MARKET_SOURCE=shioaji`。
  → ⑧-c ✅ `ShioajiSource`（2026-11-08，`collectors/shioaji_source.py`）：行情權限登入（不需憑證）、訂近月＋下一口、連續代號依
  `strategylib.roll`（0.3.0，換月日＝到期前 N 個交易日，與 tradewind 同義）的近月路由（夜盤歸下一個交易日 → 換月日當晚就是次月；不用永豐的 R1）、
  月份合約與整股直接路由、試撮／暫停／零股丟掉、張→股、SDK callback 只丟佇列（綁連線世代）、連線由 worker 管（開機不擋、失敗倍增重試、登入後失敗必登出、沒訂到算失敗、訂閱補訂、主檔重讀）、盤中沉默警告、換日重訂；`SHIOAJI_SIMULATION` 必填、模擬環境當演練（不進正式 bars）；compose 帶
  `MARKET_SOURCE`／金鑰、`INSTALL_SHIOAJI=1` build 時裝 SDK。假 SDK 測試＋官方文件 tick 範例當 fixture；**本機剩**：驗證清單 10 項、主檔補當月與次月。
  → ⑧-b ✅ 串流演練隔離（2026-11-08）：`ReplaySource` 的落地一律寫演練 DB（`REPLAY_DB`，預設 `data/replay.db`），
  `db/bars.bars_upsert(replay=True)` 拒寫正式 DB（bars 主鍵不含 source，演練會蓋掉同一分鐘的真資料；`PRODUCTION_DB` 放真資料的檔、比 inode）；
  Shioaji 落地前必須先有這條。看演練資料＝另開一個 server（另一個 port，`PREMARKET_DB`＝`REPLAY_DB`）；演練的 `/ws/bars` 跟真的長得一樣，不要讓 tradewind 的 paper 排程接。
  名詞（與 tradewind 12-engine 一致）：**串流演練**＝ReplaySource 回放進 /ws/bars；**歷史回測**＝tradewind 讀 /api/bars 跑 backtest；
  **事件重放**＝tradewind 重放錄下的外部事件、比對輸出（未實作）。
  → ⑨ ✅ `GET /api/calendar?date_from=&date_to=`（2026-11-08）：`market_holidays` 的休市日＋有資料的年份（唯讀、非契約表；讀表入口在
  `utils/trading_calendar.holidays_between`）。tradewind 的 paper 排程每天開盤前拉一次算交易日與換月日（假日單一真相在這裡）。
- 待定事項列在 `docs/contracts/README.md` 末段，等 tradewind 上 GitHub 後在雙 repo session 定稿。

---

## UI 分離（ADR-0006）

- 後端只出 JSON API（FastAPI＋OpenAPI）；UI 在獨立前端 repo（Vite＋TypeScript＋React），同一個前端吃 windscope／tradewind／錢包三個 API。
- 本 repo 不再新增 Jinja 頁；既有頁面過渡期保留，**API-first**：先補每頁對應的 `/api/*`（`/stats` 已有 `/api/call-stats`），前端做到哪頁、哪頁的 Jinja 才下線。
- 前端第一批只做兩頁：判斷有效性（windscope）、狀態頁＋kill switch（tradewind，按鈕走 ops）。
- 前端 repo＝windwatch：「今日」（windwatch #1）、「情報」（windwatch #2：判斷有效性、判斷與驗證、籌碼觀察）已做；
  `signals.html`、`stats.html` 的內容前端都有了，可排下線。
- ✅ 命中率依「驗證當下」的規則版本分（2026-11-09，windwatch #2 審查）：`verifications.rule_version` 驗證時記下，
  `call-stats` 與近 N 筆小卡（`/api/stats`、首頁、訊號頁，預設現行版本）都依它篩；之前用 signals 的現在版本篩，
  判斷驗證後被 recompute 成新版本時，舊判斷會被算進新版本。舊資料遷移只補「判斷在驗證前產生、之後沒被重算」的列，
  其他留 NULL 不猜（不列入分版統計；每次啟動都會再試補，第一次啟動中途掛掉也補得回來）；要讓它們進統計用
  `python main.py reverify --from … --to …`（離線重驗，只讀 raw_index，照現在的判斷與版本算；`--all` 重驗區間內每一筆）。
