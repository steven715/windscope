# ADR-0003：排程 job 與 backfill 不走統一的「依賴圖往下重算」入口

- **狀態**：Accepted
- **日期**：2026-10-11
- **脈絡背景**：2026-10-11 第二輪架構審查（improve-codebase-architecture）候選 #3 提議讓
  before_open／after_night／chip_collect／backfill／CLI 等 7 個呼叫端都改呼叫
  `integration/recompute`，由它依「哪個原始來源更新了」沿依賴圖往下重算。評估後決定不做；
  本 ADR 記錄理由，避免日後審查重複提出。

---

## 脈絡（Context）

衍生資料的依賴關係：

| 衍生資料 | 依賴 | 由誰、何時算 |
|---|---|---|
| 匯率指標（T） | T 日報價＋前日收盤 | before_open 08:50 |
| 期貨指標（T） | 歸屬 T 日的夜盤 | after_night 05:00 |
| 盤前判斷（T） | T 日 daily_metrics | **只在 before_open 08:50** |
| 分點指標（D） | D 日分點 | chip_collect 晚上、分點匯入 |
| 籌碼觀察（T） | T 之前的分點指標與外資 | before_open、recompute、分點匯入 |

各 job 只算「自己剛收進來的原始來源」那一層，看起來像是重複列步驟，因此被提為候選。

## 決策（Decision）

**不讓排程 job 與 backfill 沿依賴圖自動往下重算到 L3。** 各 job 繼續只算自己那一層；
`integration/recompute.recompute_date` 只供 CLI `recompute`（事後整日重導）使用。
籌碼觀察另以 `integration/chip_observation.refresh_chip_observations` 為單一入口（那是真重複，已收斂）。

## 理由（Rationale）

1. **盤前判斷是立場，只能在 08:50 產生。** 若 after_night（05:00）更新期貨指標後順勢往下算，
   會在 08:50 前寫出一筆第 T 日的 `signals`；dashboard、TG 與驗證都會看到一個不是 08:50 做出的判斷。
2. **backfill 不能憑空生出歷史盤前判斷。** backfill 補的是歷史原始資料；若連 L3 一起算，
   會替當時沒做過判斷的日子寫入 `signals`，被計入命中率，污染統計。
3. **job 的步驟順序本身就是排程語意。** 「誰在什麼時間點算到哪一層」由排程決定，不是
   可以抽掉的樣板；集中成一個依賴圖反而要在裡面加「現在幾點、是不是補資料」的旗標，interface 變寬。

## 承擔的代價（Consequences）

- 各 job 仍各自呼叫一個 L2 compute；新增原始來源時要記得在對應 job 補上那一步。
- `main.py compute`（只算 L2）與 `recompute`（含 L3）並存，用途不同。
- **守則**：任何會寫入 `signals` 的路徑，只能是 before_open 與 CLI `recompute`；新增其他寫入路徑前先回頭看本 ADR。
