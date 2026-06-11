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

# === Verification (Layer 4) ===
VERIFY_FLAT_BAND_PCT = 0.3          # |漲跌幅| <= 0.3% 視為「平」

# === Server ===
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000
SCHEDULE_AFTER_NIGHT = "05:30"
SCHEDULE_BEFORE_OPEN = "08:50"
SCHEDULE_VERIFY_CLOSE = "13:40"
SCHEDULE_AFTER_CLOSE = "18:30"
