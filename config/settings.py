import os

# DB
DB_PATH = os.environ.get("PREMARKET_DB", "data/premarket.db")

# HTTP
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
HTTP_DELAY_MIN = 1.0
HTTP_DELAY_MAX = 3.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
