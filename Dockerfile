FROM python:3.12-slim

# 排程與 log 時間都依台北時間
ENV TZ=Asia/Taipei \
    PYTHONUNBUFFERED=1 \
    PREMARKET_DB=data/premarket.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 永豐行情 SDK（MARKET_SOURCE=shioaji 才要）：build 時 INSTALL_SHIOAJI=1 才裝；版本跟 tradewind 釘同一版（介面以它的 _core.pyi 為準）
ARG INSTALL_SHIOAJI=0
RUN if [ "$INSTALL_SHIOAJI" = "1" ]; then pip install --no-cache-dir "shioaji==1.7.11"; fi

COPY . .

RUN useradd --create-home appuser \
    && mkdir -p data logs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "main.py", "serve"]
