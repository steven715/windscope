FROM python:3.12-slim

# 排程與 log 時間都依台北時間
ENV TZ=Asia/Taipei \
    PYTHONUNBUFFERED=1 \
    PREMARKET_DB=data/premarket.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home appuser \
    && mkdir -p data logs \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "main.py", "serve"]
