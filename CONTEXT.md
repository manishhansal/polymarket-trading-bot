# CONTEXT.md

> Operating manual for AI agents (Cursor, Claude Code, Codex) working on **polybot**.
> Read this **before** making changes. Humans should read [`README.md`](./README.md) instead.

---

## What this project is

`polybot` is a full-stack, production-ready Polymarket trading bot whose entire job is to compound **$5 → $1,000** by exploiting mispriced binary prediction-market contracts. It has two execution modes — paper (simulated) and live (real USDC on Polygon) — and a real-time dashboard.

**Non-goals** (do not propose changes in these directions without explicit user request):
- Algorithmic trading on non-prediction markets (equities, crypto, sports books)
- Machine-learning probability models — the edge engine is deliberately rules-based + oracle-blended
- Multi-user / multi-account support
- Order-book market-making (we are takers, not makers)

---

## Mental model

```
Polymarket Gamma API ─► scanner ─► edge_calc ─► kelly ─► executor ─► db
                                                  │                    │
                                                  └────► portfolio ◄───┘
                                                              │
              dashboard (React) ◄── WebSocket ◄── scheduler ──┘
```

Three APScheduler jobs run forever in the background:

| Job              | Cadence | Responsibility                                              |
|------------------|---------|-------------------------------------------------------------|
| `scan_and_trade` | 60 s    | scan markets → compute edge → size → route to executor       |
| `refresh_marks`  | 30 s    | refresh open-position prices, enforce stop-loss              |
| `write_snapshot` | 60 s    | append a bankroll point + emit WebSocket update             |

The FastAPI app additionally runs a 5-second heartbeat broadcaster for the dashboard.

---

## File-by-file map

### Backend (`backend/`)

| File              | Purpose                                                                                | Touch when…                                          |
|-------------------|----------------------------------------------------------------------------------------|------------------------------------------------------|
| `config.py`       | Pydantic settings, `.env` loading, the **STAGES** compounding table                    | adding a tunable knob or a new stage                 |
| `db.py`           | SQLModel schema, engine, KV helpers                                                    | adding a column or table                             |
| `kelly.py`        | Kelly fraction + `size_position` gating                                                | changing how stake size is decided                   |
| `edge_calc.py`    | Oracle fetchers (Metaculus/Kalshi/PredictIt), `_blend`, `compute_edge`                 | adding a new probability source                      |
| `scanner.py`      | Gamma API → `MarketSnapshot` → filter → rank                                           | changing what markets we consider                    |
| `executor.py`     | Paper + live (`py-clob-client-v2`) order placement, mark-to-market, position close-out | touching order routing or slippage logic             |
| `wallet.py`       | Read-only USDC + MATIC balance polling on Polygon, cached for 30 s                     | adding new on-chain reads                            |
| `portfolio.py`    | Dual bankroll, PnL, drawdown, `effective_mode()`, stage transitions, alerts, stats     | adding a risk gate or KPI                            |
| `scheduler.py`    | APScheduler job graph + WebSocket broadcaster registration                             | adding/removing a background job                     |
| `main.py`         | FastAPI app: REST routes, WebSocket endpoint, lifespan, serializers                    | adding an API endpoint                               |
| `tests/`          | `pytest -q` runs all of these                                                          | every feature change ships with tests                |

### Frontend (`frontend/src/`)

| File / dir              | Purpose                                                            |
|-------------------------|--------------------------------------------------------------------|
| `App.jsx`               | Layout, polls REST + listens to WS, refreshes all panels           |
| `main.jsx`, `index.css` | Entry + Tailwind base + the `panel`/`pill`/`btn-*` component CSS    |
| `lib/api.js`            | Single fetch wrapper. All HTTP goes through `api.<method>()`        |
| `lib/format.js`         | `fmtUSD`, `fmtPct`, `fmtTime`, `fmtDate`, `fmtSigned`               |
| `hooks/useWebSocket.js` | Auto-reconnecting WS hook, exposes `{ connected, lastEvent, send }` |
| `components/*.jsx`      | One file per dashboard panel                                       |

---

## Critical invariants — do not break these

1. **PAPER is the always-safe path.** Every change must work without `POLYMARKET_API_KEY` or `PRIVATE_KEY` set. When credentials are missing, the bot **must** fall through to the paper executor (never crash, never block). In auto mode this is the default behaviour — `effective_mode()` returns `PAPER` whenever the wallet is unreachable. The default `TRADING_MODE` is **`auto`**, not `paper`, because auto is paper-when-unfunded *and* live-when-funded with no per-trade decision the user has to make.
2. **Kelly formula must stay correct.** The closed form for a binary Polymarket YES contract is
   `f* = (m − p) / (1 − p)`, where `m` is model probability and `p` is market price. Do **not** rewrite this as `(m / p) − 1` — that's wrong and was already caught and fixed once. The textbook-equivalence test in `test_kelly.py::test_kelly_matches_textbook_formula` exists to prevent regressions.
3. **Never fabricate edge.** When `edge_calc.compute_edge` can't get a probability from any oracle, the answer is **always** `model_probability = yes_price`, `edge = 0`, `confidence = 0` (sources tagged `["no-oracle"]`). Heuristic "mean reversion toward 0.50" was tried, surfaced phantom 3-5% edge on every tail-priced market, and was removed on 2026-05-30. Test: `test_edge_calc.py::test_no_oracle_match_produces_zero_edge_and_zero_confidence`. **Do not reintroduce heuristic edge without independent calibration evidence.**
4. **Money never leaks.** `executor.py` is the **only** module allowed to call `py_clob_client` or sign anything. Every other module composes intent (sizing, scoring, ranking) without touching the wallet.
5. **All secrets come from `.env`.** Never hard-code an API key, private key, or RPC URL in source. `Settings` already covers every secret we need — add new ones there.
6. **All API responses must be strict-JSON compliant.** No `inf`, `NaN`, `-inf` may leak into a response. Use `_json_safe_number` in `main.py` to coerce. Test: `test_api_serialization.py::test_config_endpoint_serializes_without_inf`.
7. **Database schema is PostgreSQL-compatible.** No SQLite-specific column affinity tricks, no `JSON1` extension usage. We use SQLite in dev only.
8. **The circuit breaker is sacred.** `portfolio.trading_allowed()` is consulted by `scheduler.scan_and_trade` before any new position is opened. Do not bypass it.
9. **WebSocket is fire-and-forget.** `scheduler` calls `_broadcast(...)` via a registered callable so it never imports FastAPI. Preserve this seam — it avoids circular imports.

---

## Trading-mode semantics

Three configured modes, two effective modes. The **configured** mode comes from `TRADING_MODE` in `.env`; the **effective** mode is what `executor.py` actually uses for the next trade and is resolved by `portfolio.effective_mode(wallet)`.

| Configured | Effective when wallet ≥ threshold | Effective when wallet < threshold | Effective when wallet unreachable |
|------------|------------------------------------|------------------------------------|------------------------------------|
| `paper`    | PAPER                              | PAPER                              | PAPER                              |
| `live`     | LIVE                               | LIVE (will still try to spend $0!) | LIVE (will fail at executor)       |
| `auto`     | LIVE                               | PAPER                              | PAPER                              |

`auto` is the default. The threshold is `AUTO_MODE_MIN_BALANCE_USD` ($5 by default).

Two **independent ledgers** are maintained at all times:
- The **paper bankroll** starts at `INITIAL_BANKROLL` and accumulates the PnL of all `Trade.mode == PAPER` rows + paper open positions.
- The **live bankroll** equals the on-chain USDC balance plus the mark-to-market value of any open `Trade.mode == LIVE` positions.

Both are surfaced in `PortfolioState.paper_bankroll` and `.live_bankroll` and both render in the dashboard header when in AUTO mode.

When auto mode crosses the threshold in either direction, `scheduler.poll_wallet` writes a `SettingsKV` flag (`last_effective_mode`) and pushes a `SUCCESS` (going live) or `WARNING` (reverting to paper) alert. Don't bypass that — the user must always know when real money is about to move.

### Python

- **3.11+ only.** We use `from __future__ import annotations` everywhere for cheap forward refs.
- **Type hints on every public function**, including return types.
- **`loguru` for logging.** Never use `print`. Don't add a second logging framework.
- **`asyncio` everywhere on the hot path.** Scheduler jobs are async; HTTP is `httpx.AsyncClient`; CLOB calls are wrapped in `asyncio.to_thread`.
- **Settings are singletons** via `@lru_cache` on `get_settings()`. Tests call `.cache_clear()` between tests — preserve this pattern when adding fixtures.
- **No comments narrating obvious code.** Only explain non-obvious *why* (e.g. "use min() because stage-multiplier may be more aggressive than the global Kelly cap").

### JavaScript / React

- **JSX only**, no TypeScript (intentional — keeps the dashboard hackable).
- **Tailwind utility classes** plus a small set of semantic components defined in `index.css` (`panel`, `pill`, `btn-primary`, …). Use them instead of bespoke classNames.
- **One panel per file** under `components/`.
- **State stays in `App.jsx`.** Children are pure-presentation. The only "side effects" inside a child component are the `<input onChange>` handlers and POST calls via `lib/api.js`.
- **`clsx`** for conditional classNames. No `classnames`, no inline ternaries longer than 2 branches.

### Colors (Tailwind)

| Use                                | Class                             |
|------------------------------------|-----------------------------------|
| Positive PnL, success, live ws     | `text-term-green`                 |
| Negative PnL, error, live mode     | `text-term-red`                   |
| Paper mode, warnings               | `text-term-amber`                 |
| Edges, links, neutral highlights   | `text-term-blue`                  |
| Opportunities                      | `text-term-purple`                |
| Default / metadata                 | `text-term-gray`                  |

Never use raw hex codes outside `tailwind.config.js`.

---

## Testing rules

- **Every change to `kelly.py`, `edge_calc.py`, or `executor.py` requires a corresponding test.** Run `pytest -q`; expect green.
- Tests use the `fresh_db` fixture, which drops + recreates all SQLModel tables against the existing engine. **Do not call `importlib.reload` on `backend.db`** — it double-registers tables with SQLAlchemy's `MetaData` and explodes.
- Async tests are auto-discovered (`asyncio_mode = auto` in `pytest.ini`). No `@pytest.mark.asyncio` decorator needed on every test, but adding one is harmless.
- Mock external HTTP via `monkeypatch.setattr(edge_calc, "_fetch_metaculus_match", ...)` etc. Do **not** hit real Metaculus / Kalshi / PredictIt from tests.
- There are no frontend tests yet. If you add some, use Vitest + React Testing Library — do not introduce Jest.

---

## How the bot grows the bankroll

The four stages in `config.STAGES` (`Ignition → Acceleration → Cruise → Preservation`) are the *only* place stage-specific behaviour is defined. Adding a new stage means appending one dict and nothing else — `get_active_stage(bankroll)` picks the right one, `kelly.size_position` reads `kelly_fraction` / `min_edge` / `max_positions` from it, and `portfolio.snapshot_bankroll` emits a `StageEvent` + alert on transition.

If you're tempted to add stage-specific code branches elsewhere, **don't**. Push the variability into the stage dict.

---

## Common tasks

### Adding a probability oracle

1. Add an env var for the host in `Settings` (`config.py`).
2. Write `_fetch_<source>_match(question, client)` in `edge_calc.py` that returns `Optional[float]` ∈ (0, 1).
3. Append it to the loop in `compute_edge`.
4. Add a `monkeypatch`-based test in `test_edge_calc.py`.
5. Document it in the README's "How it makes money" → "Edge calculator" section.

### Adding a dashboard panel

1. Create `frontend/src/components/<Name>.jsx`. Use existing components as a template — every panel has a `panel` wrapper, a `panel-header`, and a `panel-body`.
2. Wire its data via a new field in `App.jsx`'s `Promise.all([...])` refresh and a new method on `api`.
3. Add a `GET /api/<name>` endpoint in `main.py` if you need new server state. Use a `_serialize_<thing>` helper for the response shape — never return ORM objects directly.

### Adding a runtime-mutable setting

1. Use the `SettingsKV` table — read with `get_kv(key, default)`, write with `set_kv(key, value)`.
2. Surface it in `GET /api/config` and accept it in the `POST /api/config` body model.
3. Add a slider/input to `ConfigPanel.jsx`.

### Shipping a live-mode change

- Test it in paper mode first by setting `TRADING_MODE=paper` and reading the trade log.
- Confirm `executor._execute_live` still aborts cleanly when the slippage guard trips — it has saved real money before.
- The `confirm: true` flag on `POST /api/mode` is the user-facing safety net — don't loosen it.

---

## Things I am not allowed to do without asking

- Switch the dashboard to TypeScript, Next.js, or a different chart library.
- Replace SQLModel with raw SQLAlchemy or any other ORM.
- Add a real ML model (`scikit-learn`, `torch`, …) for edge calculation.
- Add Celery / Redis / RabbitMQ. APScheduler is sufficient at this scale.
- Replace `py-clob-client-v2` with a hand-rolled CLOB integration.
- Downgrade to V1 `py-clob-client` — it was archived and broken after the V2 cutover in 2026.
- Add user authentication. The dashboard is local-only by design.
- Modify `.env` directly (only `.env.example` is tracked).
- Commit anything to git unless the user explicitly asks.

---

## Gotchas observed in development

- **`backend.db.engine` is module-level.** The cached settings singleton determines the DB URL at first import — that's why `conftest.py` sets `DATABASE_URL` **before** any `from backend...` import.
- **APScheduler + asyncio**: jobs are coroutines; `coalesce=True` and `max_instances=1` are both set on every job to prevent overlapping runs during slow Polymarket responses.
- **Polymarket Gamma API returns JSON-encoded strings inside JSON** (e.g. `outcomePrices: "[\"0.45\", \"0.55\"]"`). `scanner._parse_market` handles the double-decode — don't naïvely treat them as Python lists.
- **Mode toggle requires a backend restart.** This is intentional: `get_settings()` is `@lru_cache`d, so a hot toggle would leave half the system in the old mode. The dashboard already prompts the user.
- **Pydantic v2 only.** `field_validator` not `validator`, `model_config` not `class Config`.
- **Oracle reality (2026)**: Metaculus closed its public API in 2025 — `_fetch_metaculus_match` requires `METACULUS_TOKEN` and silently returns None otherwise. Kalshi's *public* endpoint is `api.elections.kalshi.com/trade-api/v2` (NOT `trading-api.kalshi.com` — that's auth-only now). Despite the "elections" subdomain it serves every Kalshi market. PredictIt was removed entirely (the platform shut down for US users in 2024). When all sources fail, the scheduler emits a one-shot "All oracles unavailable" warning alert and the bot patiently does nothing.
- **`float('inf')` in JSON**: `config.STAGES` uses `float('inf')` as Stage 4's ceiling. Vanilla `json.dumps` rejects it. The `/api/config` endpoint passes stages through `_serialize_stage` which coerces inf/NaN to `None`.
- **Polymarket CLOB V2 migration (2026)**: The original `py-clob-client` is archived/non-functional. We use `py-clob-client-v2`. Method name changes worth noting: `create_or_derive_api_creds()` → `create_or_derive_api_key()`; `client.create_and_post_order(order)` → `client.create_and_post_order(order_args=..., options=PartialCreateOrderOptions(tick_size="0.01"), order_type=OrderType.GTC)`; `side="BUY"` → `side=Side.BUY`. Tick size is required.
- **Collateral token migration to pUSD (April 28, 2026)**: Polymarket replaced USDC.e with **pUSD** (`0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`) as the settlement asset. CLOB V2 orders are signed against pUSD. `wallet.py` reads BOTH `pusd_balance` (the trade-relevant one — gates auto-mode) AND `usdce_balance` (informational — drives the "needs wrap" hint). `effective_mode()` checks pUSD only; USDC.e sitting unwrapped contributes nothing to the threshold. The CollateralOnramp at `0x93070a847efEf7F70739046A929D47a521F5B8ee` wraps USDC.e → pUSD 1:1 — wrap via `scripts/wrap_usdc.py` for API-only flows, or visit polymarket.com once and the UI wraps automatically.
- **Generating Polymarket API keys**: There is no Polymarket "developer portal" UI for keys — they are derived from an EIP-712 signature with the wallet's private key. Users run `python scripts/generate_polymarket_keys.py` which calls `create_or_derive_api_key()` and pastes the result into `.env`. The operation is **idempotent** — running it twice for the same wallet returns the same keys, so it's safe to re-run.

---

## Useful commands

```bash
# Run the full test suite
.venv/Scripts/python.exe -m pytest backend/tests -q

# Boot just the backend (auto-reload)
uvicorn backend.main:app --reload --port 8000

# Boot just the frontend
cd frontend && npm run dev

# Both, dockerized
docker compose up --build

# Force an immediate market scan (bypasses the 60s cron)
curl -X POST http://localhost:8000/api/scan

# Pause / resume trading
curl -X POST http://localhost:8000/api/pause -H 'content-type: application/json' -d '{"paused": true}'
```

---

## When in doubt

- **Read the failing test first** — `pytest -q` output usually pinpoints the misunderstanding.
- **Check `STAGES` in `config.py`** before changing any sizing or filter behaviour.
- **The Kelly closed form is `(m − p) / (1 − p)`. Always. Forever.**
