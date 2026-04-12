# 資料來源文件（Data Sources）

本文件記錄 Phase 1 所有資料來源的 URL、格式、可靠性、驗證狀態。
每次新增或修改來源時，同步更新此文件。

---

## 狀態定義

| 狀態 | 意義 |
|------|------|
| ✅ VERIFIED | 已實測可用，parser 已實作 |
| 🔧 PARTIAL | URL 可達，但 parser 尚未完成或有已知問題 |
| ❓ UNVERIFIED | 尚未實測，URL 和格式來自文件推測 |
| 🚫 BLOCKED | 已確認無法使用（被擋、需付費、格式不符） |
| 📌 STUB | 程式中已留 stub，等待替換 |

---

## 一、證交所（TWSE）

### 1.1 三大法人買賣超（大盤）

| 項目 | 內容 |
|------|------|
| 用途 | 取得外資/投信/自營商每日買賣超金額 |
| URL | `https://www.twse.com.tw/rwd/zh/fund/BFI82U?date={YYYYMMDD}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日 ~15:00 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 證交所資料格式歷史上很穩定。`stat` 欄位為 `"OK"` 表示有資料；非交易日回傳 `"很抱歉，沒有符合條件的資料"`。|

**回應結構（預期）：**
```json
{
  "stat": "OK",
  "title": "三大法人買賣金額統計表...",
  "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
  "data": [
    ["外資及陸資(不含外資自營商)", "...", "...", "..."],
    ...
  ]
}
```

---

### 1.2 每月加權指數行情

| 項目 | 內容 |
|------|------|
| 用途 | 取得加權指數每日收盤價（`spot_close`） |
| URL | `https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={YYYYMMDD}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日收盤後 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 回傳整個月的資料，需從 `data` 陣列中找到目標日期那一列。日期格式為民國年（如 `115/04/08`）。收盤價在第 5 欄（index 4）。|

---

### 1.3 個股每日收盤價

| 項目 | 內容 |
|------|------|
| 用途 | 取得觀察名單個股的收盤價，用於籌碼金額計算和 MA20 |
| URL | `https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={YYYYMMDD}&stockNo={stock_id}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日收盤後 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 回傳整個月的日行情。收盤價在第 7 欄（index 6）。日期為民國年格式。每次只能查一檔，watchlist 多檔時需逐一查詢並加 delay。|

---

### 1.4 外資個股買賣超

| 項目 | 內容 |
|------|------|
| 用途 | 取得外資對觀察名單個股的每日買賣超 |
| URL | `https://www.twse.com.tw/rwd/zh/fund/T86?date={YYYYMMDD}&selectType=ALL&response=json` |
| 方法 | GET |
| 回傳格式 | JSON |
| 更新時間 | 每個交易日 ~15:30 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 回傳全市場外資買賣超，資料量大。需從中篩選 watchlist 中的 stock_id。`selectType=ALL` 取全部；也可用 `selectType=ALLBUT0999` 排除 ETF。|

---

### 1.5 除息預估點數

| 項目 | 內容 |
|------|------|
| 用途 | 取得當日除息對加權指數的預估影響點數，用於調整期貨價差 |
| URL | `https://www.twse.com.tw/rwd/zh/exRight/TWT49U?date={YYYYMMDD}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON（待確認） |
| 更新時間 | 交易日（僅有除息日才有資料） |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 此 URL 為推測，需實測確認。可能需要從其他路徑取得。非除息日回傳空值是正常的（預估點數 = 0）。如果此 API 不存在，備案是從證交所「除權除息預告表」手動查詢或爬取。|

---

## 二、期交所（TAIFEX）

### 2.1 期貨每日行情（含夜盤）

| 項目 | 內容 |
|------|------|
| 用途 | 取得台指期夜盤收盤價和成交量 |
| URL | `https://www.taifex.com.tw/cht/3/futContractsDate` |
| 方法 | POST（表單查詢） |
| 回傳格式 | HTML |
| 更新時間 | 夜盤收盤後（次日 ~05:15） |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 這是表單頁面，需要 POST 以下參數：`queryType=1`、`marketCode=0`、`dateaddcnt=0`、`commodity_id=TX`、`queryDate={YYYY/MM/DD}`。回傳 HTML 表格需用 BeautifulSoup 解析。夜盤資料可能標記為「盤後」或合併在「一般+盤後」欄位中。|

**替代方案：**
- 期交所 CSV 下載：`https://www.taifex.com.tw/cht/3/futContractsDateDown`（POST，同參數，回傳 CSV）
- 如果有 CSV 版本優先使用，比 HTML parsing 更穩定

---

### 2.2 三大法人期貨留倉（外資未平倉）

| 項目 | 內容 |
|------|------|
| 用途 | 取得外資台指期未平倉淨額（口數） |
| URL | `https://www.taifex.com.tw/cht/3/futContractsDate` 或 `https://www.taifex.com.tw/cht/3/largeTraderFutQry` |
| 方法 | POST 或 GET（待確認） |
| 回傳格式 | HTML |
| 更新時間 | 每個交易日 ~15:00 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 需取得「外資」在「臺股期貨」的多方、空方、淨額口數。確切的頁面路徑和欄位位置需實測確認。也可能在 `https://www.taifex.com.tw/cht/3/totalTableDate` 頁面中。|

---

## 三、匯率（FX）

### 3.1 台灣銀行牌告匯率

| 項目 | 內容 |
|------|------|
| 用途 | 取得 USD/TWD 即時牌告匯率（08:45 報價、16:00 收盤） |
| URL（網頁版） | `https://rate.bot.com.tw/xrt?Lang=zh-TW` |
| URL（CSV） | `https://rate.bot.com.tw/xrt/flcsv/0/day` |
| 方法 | GET |
| 回傳格式 | HTML 或 CSV |
| 更新時間 | 營業日即時更新 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 台銀牌告是最穩定的 TWD 匯率來源。CSV 版本（如果可用）優先使用。注意：牌告匯率有「現金買入/賣出」和「即期買入/賣出」之分，我們要的是「即期買入」（銀行買入美元的價格 ≈ 市場 USD/TWD 匯率）。|

---

### 3.2 鉅亨網匯率

| 項目 | 內容 |
|------|------|
| 用途 | USD/TWD、USD/CNY、USD/KRW 即時匯率（方案 B） |
| URL | `https://www.cnyes.com/forex/detail/USD-TWD/overview` |
| 方法 | GET |
| 回傳格式 | HTML（可能有 JS 渲染） |
| 更新時間 | 即時 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 鉅亨網頁面可能使用 SPA 架構，直接 requests.get 可能拿不到匯率數值。如果是 JS 渲染，需要找到底層的 API endpoint（通常是 XHR/Fetch 請求），而非解析 HTML。可嘗試抓取 `https://ws.api.cnyes.com/ws/api/v1/charting/history?symbol=USD-TWD&resolution=5` 之類的 API（URL 為推測）。|

---

### 3.3 Yahoo Finance 匯率

| 項目 | 內容 |
|------|------|
| 用途 | USD/CNY、USD/KRW 即時報價（也可取 USD/TWD） |
| URL | 透過 `yfinance` 套件或直接呼叫 Yahoo Finance API |
| 幣對代碼 | `USDTWD=X`, `USDCNY=X`, `USDKRW=X` |
| 方法 | API / HTTP |
| 狀態 | ❓ UNVERIFIED |
| 備註 | Yahoo Finance 有時會擋非瀏覽器的請求。如果用 `yfinance` 套件注意它會拉進 `pandas` 依賴——如果為了避免 pandas，可以直接用 Yahoo Finance v8 API：`https://query1.finance.yahoo.com/v8/finance/chart/USDTWD=X?interval=1d&range=5d`。此 API 穩定性中等，可能需要 cookie/crumb 機制。|

---

## 四、分點籌碼

### 4.1 證交所券商買賣日報

| 項目 | 內容 |
|------|------|
| 用途 | 取得個股分點進出明細 |
| URL | `https://www.twse.com.tw/rwd/zh/fund/TWT43U?date={YYYYMMDD}&stockNo={stock_id}&response=json` |
| 方法 | GET |
| 回傳格式 | JSON（待確認） |
| 更新時間 | 每個交易日 ~18:00 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 這是最理想的來源——官方、穩定、JSON 格式。但需確認此 URL 是否真的回傳個股的券商買賣明細（而非大盤）。如果此 API 存在，回傳的應該是每個券商對該股票的買進/賣出張數。|

---

### 4.2 富邦看盤（備選）

| 項目 | 內容 |
|------|------|
| 用途 | 個股分點進出明細（備選方案） |
| URL | `https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco/zco_{stock_id}.djhtm` |
| 方法 | GET |
| 回傳格式 | HTML |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 第三方來源，可能隨時改版或被擋。HTML 解析較不穩定。如有證交所 JSON API 可用則不需此來源。|

---

### 4.3 手動 CSV 匯入（保底方案）

| 項目 | 內容 |
|------|------|
| 用途 | 當所有自動來源不可用時，由使用者手動匯入 |
| 格式 | CSV，欄位：`date,stock_id,stock_name,broker_name,buy_volume,sell_volume,net_volume` |
| 狀態 | 📌 STUB（需實作 CSV import 功能） |
| 備註 | 使用者可以從籌碼 K 線 APP 匯出或手動整理。CLI 指令：`python main.py import-chip path/to/chip.csv`。這是最可靠的 fallback——資料來源不穩定時至少還能手動維持系統運作。|

---

## 五、進階來源（Phase 1 非必要）

### 5.1 富台指（FTSE TWSE Taiwan 50 Futures）

| 項目 | 內容 |
|------|------|
| 用途 | 台指期 vs 富台指的價差比對 |
| 可能來源 | SGX（新加坡交易所）、財經網站 |
| 狀態 | ❓ UNVERIFIED |
| 備註 | SGX 資料可能需要付費。Phase 1 先留 `NULL`，不影響核心功能。`raw_futures.ftse_tw_close` 欄位預留但不強制。|

### 5.2 紐約盤 USD/TWD 收盤價

| 項目 | 內容 |
|------|------|
| 用途 | 對照紐約盤 vs 台北盤的匯率差異 |
| 可能來源 | Reuters、鉅亨網、Bloomberg |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 免費來源可能不存在。Phase 1 先留 `NULL`。`raw_fx.ny_close` 欄位預留但不強制。|

### 5.3 S&P 500 收盤

| 項目 | 內容 |
|------|------|
| 用途 | 美股收盤對照（台指期夜盤 vs 美股是否同步） |
| URL | 同 Yahoo Finance：`https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d&range=5d` |
| 狀態 | ❓ UNVERIFIED |
| 備註 | 與 FX 共用 Yahoo Finance 來源。代碼 `^GSPC`（URL encode 為 `%5EGSPC`）。|

---

## 來源優先順序總結

| 資料 | 首選 | 備選 | 保底 |
|------|------|------|------|
| 三大法人 | 證交所 JSON API | — | — |
| 加權指數收盤 | 證交所 JSON API | — | — |
| 個股收盤價 | 證交所 JSON API | — | — |
| 外資個股買賣超 | 證交所 JSON API | — | — |
| 除息點數 | 證交所 JSON API | — | 手動輸入 |
| 期貨夜盤 | 期交所 CSV/HTML | — | — |
| 外資期貨未平倉 | 期交所 HTML | — | — |
| USD/TWD 匯率 | 台灣銀行牌告 | 鉅亨網 | 手動輸入 |
| USD/CNY 匯率 | Yahoo Finance | 鉅亨網 | 手動輸入 |
| USD/KRW 匯率 | Yahoo Finance | 鉅亨網 | 手動輸入 |
| 分點籌碼 | 證交所券商日報 | 富邦看盤 | CSV 手動匯入 |
| S&P 500 | Yahoo Finance | — | — |
| 富台指 | — | — | Phase 1 略過 |
| 紐約盤匯率 | — | — | Phase 1 略過 |

---

## 變更紀錄

| 日期 | 變更內容 |
|------|----------|
| 2026-04-12 | 初版建立，所有來源標記為 UNVERIFIED |
