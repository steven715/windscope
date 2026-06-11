# v2: Server + Signal Engine + Verification Loop — Implementation Plan

> **For agentic workers:** Steps use checkbox (`- [ ]`) syntax for tracking. Run `pytest` after each round; commit per round.

**Goal:** Evolve the Phase 1 batch pipeline into a self-running server with a daily closed loop: collect → metrics → 08:50 signal (direction + confidence + reasons) → post-close verification (hit/miss + accumulated hit rate), with a web dashboard and query pages.

**Architecture:** Existing Layer 1/2 untouched as the engine core. New Layer 3 (`integration/signal_engine.py`) and Layer 4 (`integration/verification.py`) are pure compute modules (read DB, write DB) following the `compute_*_metrics` pattern. New `server/` package wraps everything: FastAPI app, APScheduler replacing crontab, Jinja2 server-rendered pages. Docker for deployment.

**Tech Stack:** FastAPI, uvicorn, APScheduler, Jinja2 (added); everything else unchanged.

**Design decisions (from discussion on 2026-06-12, thresholds from the founding article):**

- Market signal: FX vote (|Δ| ≥ 0.1 TWD) + futures vote (adjusted spread ±100 pts) → direction; base confidence 3 (both agree) / 2 (one neutral) / 1 (conflict → forced neutral). Modifiers ±1 each, clamped 1–5: asia sync aligned +1; TWD-only move −1; night volume ratio ≥1.5 +1 / ≤0.7 −1; foreign OI net short >30,000 lots against a bullish signal −1 (and vice versa); KRW divergence (TWD+CNY depreciating, KRW appreciating) forced −1 + warning reason.
- Stock signals (per watchlist stock): exclude `both_sides_flag=1` (fake volume); flag day-trade brokers (隔日沖) as "do not chase"; consecutive buy ≥3 days + net amount ≥ 50M TWD + price zone → `bottom_watch` (low) / `distribution_warning` (high) / `accumulation` (consolidation + ≥5 days); consecutive sell ≥3 days → `avoid`.
- Every signal row records `rule_version` so hit-rate stats stay comparable across threshold tuning.
- Verification: dual baseline, both stored, stats computed separately — primary = day change (close vs prev close), secondary = open gap (open vs prev close). Actual classified 3-way: up > +0.3% / down < −0.3% / flat between. Hit = predicted class == actual class.
- TAIEX open price source: TWSE `MI_5MINS_HIST` (verified live 2026-06-12, returns monthly daily OHLC, ROC dates, comma numbers) → new `raw_index` table.

---

## Round 1: Schema + settings + TAIEX OHLC collector

- [ ] `db/schema.py`: add tables
  - `raw_index (date PK, open, high, low, close, collected_at)`
  - `signals (date PK, direction, confidence, fx_vote, futures_vote, reasons TEXT/JSON, rule_version, created_at)`
  - `stock_signals (date, stock_id, broker_name, category, reasons, rule_version, created_at, PK(date, stock_id, broker_name))`
  - `verifications (date PK, predicted_direction, confidence, prev_close, open, close, open_gap_pct, day_change_pct, open_gap_class, day_change_class, hit_day, hit_open, verified_at)`
- [ ] `config/settings.py`: SIGNAL_RULE_VERSION, FUTURES_SPREAD_THRESHOLD=100, VOLUME_RATIO_HIGH=1.5, VOLUME_RATIO_LOW=0.7, OI_BEARISH_THRESHOLD=-30000, STOCK_NET_AMOUNT_MIN=5e7, STOCK_CONSECUTIVE_MIN=3, STOCK_ACCUMULATION_MIN=5, VERIFY_FLAT_BAND_PCT=0.3, server/scheduler constants
- [ ] `collectors/twse.py`: `collect_index_ohlc(date)` via MI_5MINS_HIST + `save_index_ohlc`; wire into `run()`
- [ ] Fixture `tests/fixtures/twse/mi_5mins_hist_202606.json` (real response, fetched 2026-06-12)
- [ ] Tests: schema (new tables + idempotent), collector parse (normal/no-data/date-missing)
- [ ] `docs/data_sources.md`: add MI_5MINS_HIST entry (VERIFIED)
- [ ] pytest green → commit

## Round 2: Layer 3 signal engine

- [ ] `integration/signal_engine.py`:
  - `compute_market_signal(date, conn) -> dict | None` — reads daily_metrics (+ prev OI), votes, modifiers, clamps, writes `signals` with reasons list (JSON) and rule_version
  - `compute_stock_signals(date, conn) -> list[dict]` — reads daily_stock_metrics + broker_tags, filter then classify, writes `stock_signals`
- [ ] Tests: vote synthesis matrix, each modifier in isolation, clamp bounds, KRW divergence forcing, missing-metrics → None, stock filter/classification each branch
- [ ] Wire into `jobs/before_open.py` after fx metrics step; signal text appended to summary
- [ ] pytest green → commit

## Round 3: Layer 4 verification engine

- [ ] `integration/verification.py`:
  - `verify_signal(date, conn) -> dict | None` — needs signals row + raw_index of date + prev trading day close; classify, compare, write `verifications`
  - `get_verification_stats(conn, last_n=20) -> dict` — overall hit rate (day/open), by-confidence breakdown
- [ ] New job `jobs/verify_close.py` (13:40): collect index OHLC then verify today's signal
- [ ] Tests: band boundaries (+0.3/−0.3 exact), hit/miss per direction, neutral-hit-flat, missing signal/index, stats math
- [ ] CLI: `run verify-close`, `query signals/verifications`
- [ ] pytest green → commit

## Round 4: FastAPI server + APScheduler + pages

- [ ] `server/app.py` (app factory), `server/scheduler.py` (jobs: 05:30 after-night, 08:50 before-open+signal+notify, 13:40 verify-close, 18:30 after-close; trading-day guard inside jobs already)
- [ ] Routes/pages: `/` dashboard (today signal + cards + stats), `/signals` (history + verification table), `/data/raw`, `/data/metrics` (date-range query), `/watchlist`, `/scheduler` (job list + last/next run); `/api/*` JSON equivalents
- [ ] `main.py serve` command (uvicorn)
- [ ] Tests with TestClient + temp DB: each route 200 + key content, API range queries, empty states
- [ ] pytest green → commit

## Round 5: Notify

- [ ] `utils/notify.py`: provider interface; `LogNotifier` (default), `TelegramNotifier` (env-config: token + chat_id; uses http_client); send 08:50 summary+signal, 13:40 verification result
- [ ] Tests: dispatch logic, telegram payload (mock http), missing-config → log fallback
- [ ] pytest green → commit

## Round 6: Docker

- [ ] `Dockerfile` (python:3.12-slim, non-root), `docker-compose.yml` (restart unless-stopped, TZ=Asia/Taipei, volumes data/ logs/, port 8000)
- [ ] `.dockerignore`
- [ ] README/docs: run instructions; deprecate crontab.example (keep with note)
- [ ] Verify: build + `docker compose up` + hit dashboard
- [ ] commit
