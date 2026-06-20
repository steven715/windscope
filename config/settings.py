import os

# DB
DB_PATH = os.environ.get("PREMARKET_DB", "data/premarket.db")

# HTTP
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_DELAY_MIN = 1.0
HTTP_DELAY_MAX = 3.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# === FX Thresholds ===
FX_THRESHOLD_TWD = 0.1
FX_THRESHOLD_CNY = 0.005
FX_THRESHOLD_KRW = 5.0
# 盤前匯率節奏（原文第一件事①）：跳空＝08:45 vs 前日16:00 變動 ≥ 0.05（5分）；
# 急拉＝最近 5 分 K 單根變動 ≥ 0.03（3分）；取最近 FX_INTRADAY_BARS 根看形狀。
FX_GAP_THRESHOLD = 0.05
FX_INTRADAY_SURGE = 0.03
FX_INTRADAY_BARS = 12
# 避險情緒溫度計（USD/JPY，獨立維度，不進亞幣同步/訊號）：
# 日圓是避險/套利貨幣，急升(USD/JPY 下跌 ≥ 此值) → risk-off、對股市偏空警示。
JPY_RISKOFF_DELTA = 1.0

# === Futures ===
FUTURES_VOLUME_LOOKBACK = 5

# === Chip ===
CHIP_MA_PERIOD = 20
CHIP_MA_MIN_DAYS = 5
PRICE_ZONE_LOW = -20
PRICE_ZONE_CONSOLIDATION = 5
PRICE_ZONE_HIGH = 20

# === Signal Engine (Layer 3) ===
# 門檻來源：專案發想文章的經驗值。調整任何門檻時記得 bump SIGNAL_RULE_VERSION，
# 否則新舊規則的命中率會混在一起統計。
SIGNAL_RULE_VERSION = "v1"
FUTURES_SPREAD_THRESHOLD = 100      # 調整後價差 ±100 點才算有方向
VOLUME_RATIO_HIGH = 1.5             # 夜盤量比 >= 1.5 → 大戶佈局，信心 +1
VOLUME_RATIO_LOW = 0.7              # 夜盤量比 <= 0.7 → 觀望，信心 -1
OI_BEARISH_THRESHOLD = -30000       # 外資淨空單超過 3 萬口，偏多訊號信心 -1
OI_BULLISH_THRESHOLD = 30000        # 外資淨多單超過 3 萬口，偏空訊號信心 -1
CONFIDENCE_MIN = 1
CONFIDENCE_MAX = 5

# === Stock Signals ===
STOCK_NET_AMOUNT_MIN = 5e7          # 買超金額門檻：5,000 萬
STOCK_CONSECUTIVE_MIN = 3           # 連買/連賣天數門檻
STOCK_ACCUMULATION_MIN = 5          # 盤整區吸籌的連買天數門檻

# === 外資流向個股訊號（用 T86 每檔外資買賣超，單位：張）===
# 訊號於 08:50 產出時今日 T86 未收，故用「今日之前」最新（前一交易日）的外資資料。
FOREIGN_CONSECUTIVE_MIN = 2         # 外資連買/連賣天數門檻
FOREIGN_CUM_NET_MIN = 3000          # 連續期間累計張數門檻（過濾雜訊）
FOREIGN_BIG_NET = 10000             # 單日大買/大賣門檻（如「反手大買」）

# === Verification (Layer 4) ===
VERIFY_FLAT_BAND_PCT = 0.3          # |漲跌幅| <= 0.3% 視為「平」

# === Chip 自動來源（FinMind）===
# 分點明細唯一可自動化的合法來源是 FinMind TaiwanStockTradingDailyReport，
# 需要 Sponsor 等級的 token（https://finmindtrade.com）。未設定時自動來源停用，
# 改用手動 CSV 匯入（python main.py import-chip）。
FINMIND_API_URL = "https://api.finmindtrade.com/api/v4/data"
FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "")

# === 分點截圖 OCR（階段二，視覺 LLM）===
# 未設定 ANTHROPIC_API_KEY 時，/chip-import 的截圖上傳功能停用，手動表單照常可用。
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OCR_MODEL = os.environ.get("OCR_MODEL", "claude-sonnet-4-6")

# === Notify ===
# provider: "log"（預設，寫進 log）或 "telegram"（需設定 token 與 chat_id）
NOTIFY_PROVIDER = os.environ.get("PREMARKET_NOTIFY", "log")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# === Server ===
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000
# 盤中即時行情背景刷新間隔（秒）：背景排程每隔此秒數抓一次 MIS 存入記憶體快取，
# 頁面/API 只讀快取不阻塞。
LIVE_REFRESH_SECONDS = 12
# 資料瀏覽頁每頁筆數（server-side 分頁，不一次拉全部）
DATA_PAGE_SIZE = 50
SCHEDULE_AFTER_NIGHT = "05:30"
SCHEDULE_BEFORE_OPEN = "08:50"
SCHEDULE_AFTERNOON_FX = "16:00"   # 收盤匯率：16:00 FX 收盤(close_16)，USD/TWD + CNY/KRW/JPY
SCHEDULE_VERIFY_CLOSE = "14:30"   # 13:30 收盤後，證交所指數OHLC約需~1小時才發布，故排14:30
# 18:30：三大法人(T86)/外資個股/除息/期貨未平倉等盤後資料分批發布，~傍晚才齊，故留安全邊際
SCHEDULE_AFTER_CLOSE = "18:30"
SCHEDULE_CHIP_COLLECT = "18:00"   # 籌碼分點收集(個股收盤+分點+算指標)；預設停用，串好來源再開
