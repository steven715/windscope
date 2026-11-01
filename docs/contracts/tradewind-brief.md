# tradewind 交接簡報：上 GitHub 前要準備什麼、之後雲端與本機各做什麼

> 給 tradewind 本機 session 的開場文件。它看不到 windscope 這邊的討論，所以這份要能獨立閱讀。
> 對應的決策紀錄在 windscope repo：`docs/adr/0005-windscope-tradewind-boundary.md`；契約在 `docs/contracts/`。
> 名稱註記：以下中文層名（命題／預案／盤中反應／風控）為暫定，英文為業界對應詞，待使用者定案後統一。

---

## 0. 一句話定位

**windscope** 是資料與研究層：抓行情（含即時串流）、產命題與預案模板、收盤驗證、評分與健康監控。
**tradewind** 是交易引擎：訂閱 windscope 的行情、執行預案、做盤中反應與風控、模擬（之後真實）成交、記帳本，並把每一個事件回流給 windscope 評分。
tradewind **不做**：抓歷史資料、重算特徵、自訂還原規則、產生命題。

## 1. tradewind 擁有的四層（對照整體六層）

| 層 | 名稱（暫定 / 英文） | tradewind 做什麼 | 不能做什麼 |
|---|---|---|---|
| L2 | 預案執行 / Trade Plan | 08:50 載入 windscope 的 `targets`（命題＋預案參數），依帳戶淨值算口數、掛「不做條件」 | 不改預案內容；預案在 windscope 產、使用者盤前鎖定 |
| L3 | 盤中反應 / Trade Management | 逐根 bar 評估預案的觸發、追價上限、失效條件、分批、停損（只能收緊）、runner 出場；裁量 override 必須記錄 | **不能翻向**；不能做預案沒列的動作；只能讀少數狀態變數（價 vs 關鍵位、未實現 R、時間、量比、當日連虧數） |
| L4 | 風控 / Risk Management | 帳戶層級：日/週/月停手線、連續停損次數、獲利回吐線、資料心跳（data-age）、kill switch（觸發後**不自動重啟**） | 與策略無關；只能縮小或歸零部位，不能翻向 |
| L5 | 執行 / Execution | 目標部位 → 訂單；模擬成交（下一 tick 或下一根開盤＋滑價＋費用）；帳本（現金、部位、未實現）；重啟後從券商狀態重建、不重複下單 | 先紙上（paper），真下單等模擬盤驗證過再開 |
| 回流 | events | 成交、預案動作、override、風控觸發，每筆帶 `target_id`，回傳 windscope | — |

## 2. 契約（兩邊唯一共同讀的東西）

位置：windscope repo `docs/contracts/`（README、四份 JSON Schema、`fixtures/*.jsonl`）。tradewind 要以**固定 commit** 參照（git submodule 或 vendored 複本＋記錄 commit hash），並寫**對稱的 contract test** 讀同一份 fixture。

| 表 | 方向 | tradewind 的責任 |
|---|---|---|
| `bars` | windscope → tradewind | 歷史用 `GET /api/bars?since=`、即時用 WS 串流，**同一份 schema**；引擎只看到一條 bar 迭代器，不知道來源 |
| `instruments` | windscope → tradewind | 合約規格（tick、乘數、保證金來源、時段、換月規則）全部讀主檔，不寫死 |
| `corporate_actions` | windscope → tradewind | 只在需要還原時透過共用純函式庫用；tradewind 不自己算 |
| `targets` | windscope → tradewind | 目標部位（占 NAV 權重、信心、有效期、失效條件、預案參考）；冪等：同 key 重送同值 |
| `events` | **tradewind → windscope** | **尚未定義**，由 tradewind 這邊先提草案（見 §5） |

五條語意規則（詳見 README）：時區 `Asia/Taipei`；`available_at` 時點正確性；bars 只放原始價；targets 是目標狀態不是訂單；衍生資料不過契約。

## 3. 行情訂閱的硬規則

1. 串流走 WS（或等價 pub/sub），**不經過 DB**；格式與歷史 bars 完全相同。
2. 串流帶心跳。tradewind 的風控層必有 data-age 規則：無心跳 > 30 秒停新進、> 120 秒平倉（數字可調，規則不可缺）。
3. windscope 停機時 tradewind 必須能安全停下，不是盲跑。
4. 券商報價憑證在 windscope；下單憑證在 tradewind。兩邊不共用 secret。

## 4. 上 GitHub 前的準備清單（本機 session 現在做）

- [ ] `CLAUDE.md`：硬約束（不翻向、不重算特徵、風控不自動重啟、所有 secret 走 `.env`、契約只從 windscope 的固定 commit 讀）、技術棧（建議與 windscope 一致：Python 3.10+、stdlib `sqlite3`、不用 pandas，便於共用純函式庫；asyncio 處理串流）、目錄意圖。
- [ ] `docs/design.md`：把本機 session 討論出來的構想整理進去（cloud session 看不到對話，只看得到 repo）。
- [ ] `docs/adr/0001-consume-targets-never-recompute.md`：tradewind 只消費 `targets`，特徵一律 import 共用純函式庫，不自行重算。
- [ ] `GLOSSARY.md`：沿用 windscope 的詞（商品、K 線列、可得時刻、目標部位、共用策略庫），加上 tradewind 自己的（帳本、模擬成交、風控觸發、override）。
- [ ] `.gitignore`：`.env`、券商憑證、`data/`、`logs/`。
- [ ] `.claude/skills/`：要用的 skills（含 Matt Pocock 的）直接 commit 進 repo，cloud session 才看得到。
- [ ] 目錄骨架（建議）：`feed/`（WS client＋歷史回放）、`plan/`（載入 targets、算口數）、`manage/`（Trade Management 狀態機）、`risk/`（風控規則與 kill switch）、`exec/`（模擬成交、帳本、券商 adapter 介面）、`events/`（回流）、`contracts/`（vendored schema＋fixture＋contract test）、`tests/`。
- [ ] 第一個 contract test：讀 windscope `fixtures/targets.jsonl` 四筆，確認能解析、冪等鍵唯一、不指向 `tradable=false` 的商品。

## 5. 雲端 session 做的（純邏輯、不需券商與本機）

1. **`events` 契約草案**：建議欄位 `event_id, ts, kind(order|fill|plan_action|override|risk_trip|health), target_id, instrument_id, qty, price, reason, state_snapshot(json), schema_version`。寫成 JSON Schema＋fixture，提 PR 到 windscope `docs/contracts/`。
2. **Trade Management 狀態機**：`idle → armed → entered → scaled_out → exited`，每個轉換由預案參數驅動；以 windscope 歷史 bars 回放當測試資料。
3. **風控規則引擎**：日/週/月停手線、StoplossGuard、獲利回吐、data-age、kill switch；規則表驅動，單元測試覆蓋每條。
4. **模擬成交與帳本**：下一 tick／下一根開盤成交、滑價、費用；重啟冪等（先讀券商委託與部位再補單）。
5. **回放 harness**：從 windscope 的 `GET /api/bars` 拉歷史，用同一個引擎跑一天，產生 events；這就是「回測與模擬盤同一引擎」的證明。
6. **驗收測試**：用 windscope fixture 的四筆 targets 跑一個紙上交易日，產出的 events 能被 windscope 的 contract test 讀。

## 6. 回本機做的（需要券商、憑證、長時間常駐）

1. 券商 API adapter（永豐 Shioaji／富邦 Neo 等）：只實作介面，模擬盤階段用 windscope 的延遲行情頂著。
2. 連 windscope 的 WS 串流做實際模擬盤（paper trading）數週；每日對帳。
3. 憑證與 `.env`；常駐服務（systemd／docker compose）。
4. 真下單：等模擬盤的 events 回 windscope、評分顯示與回放一致後再開。

## 7. 驗收場景（兩邊共用，作為 MVP 的 definition of done）

30 萬台幣、微型臺指期貨（TMF，每點 NT$10）：
- 08:50 收到 target：偏多、信心 3、有效到 13:25、失效＝期貨跌破夜盤低點；預案：回測不破再上、追價上限開盤+40、1R=1%=3,000、停損 30 點 → 10 口、+1R 平半＆停損移成本、跌破前一根 5 分 K 低點出、11:30 後不新進。
- 盤中：引擎只依預案動作；風控全程在線；13:25 強制平倉。
- 盤後：events 回流，windscope 驗證命題、評分預案（回撤差）、統計 override。
- 通過標準：回放同一天歷史 bars 得到**完全相同**的 events；風控每條規則至少有一個測試讓它觸發。

## 8. 等使用者拍板的（在本機 session 先問清楚再動手）

- 保證金來源：期交所公告自動抓（windscope）還是手動維護？
- 連續合約換月規則（`roll_rule`）由 tradewind 決定：到期前幾日切？
- `target_weight` 基準：NAV（契約 v0 預設）還是可用保證金？
- `valid_until` 到期：只停新進（預設）還是自動平倉？
- 裁量 override 的允許範圍：只准提早出場？准不准減碼？一律不准加碼與翻向。
