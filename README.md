# polybot — Polymarket Viral Trading Bot

> **Mission**: Start with **$5**, compound to **$1,000** using an edge-driven Kelly Criterion strategy on Polymarket. Paper-trade safely or deploy real USDC on Polygon — toggleable from the dashboard.

![python](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)
![fastapi](https://img.shields.io/badge/fastapi-0.115-009688?logo=fastapi&logoColor=white)
![react](https://img.shields.io/badge/react-18-61DAFB?logo=react&logoColor=black)
![tailwind](https://img.shields.io/badge/tailwind-3.4-06B6D4?logo=tailwindcss&logoColor=white)
![tests](https://img.shields.io/badge/tests-59%20passing-3fb950)
![mode](https://img.shields.io/badge/default-auto%20%28paper%20when%20unfunded%29-d29922)
![license](https://img.shields.io/badge/license-MIT-blue)

> **Repository:** [github.com/manishhansal/polymarket-trading-bot](https://github.com/manishhansal/polymarket-trading-bot)

A full-stack, production-ready trading bot:
- **Python 3.11 + FastAPI** backend with APScheduler running on a 60-second cron
- **React 18 + Recharts + Tailwind** dashboard in a dark terminal aesthetic
- **WebSocket** push for sub-5-second UI updates
- **Auto mode** by default — paper-trades when the wallet is empty, automatically goes live the moment you fund it
- **Kelly Criterion** position sizing + 4-stage compounding strategy + drawdown circuit breaker
- **SQLite** dev DB with a PostgreSQL-compatible schema

> 🤖 **Working on the codebase with an AI agent?** Point it at [`CONTEXT.md`](./CONTEXT.md) first — it documents conventions, invariants, and gotchas the agent needs to stay productive (and not break the Kelly formula again).

---

## TL;DR

```bash
git clone https://github.com/manishhansal/polymarket-trading-bot.git
cd polymarket-trading-bot
cp .env.example .env
docker compose up --build
open http://localhost:5173
```

That's it. The bot is now running in **AUTO mode** with a $5.00 simulated bankroll. Without a funded wallet it stays paper — no API keys, no risk. Watch it hunt for mispriced markets every 60 seconds. Fund the wallet in `.env` with ≥ $5 of pUSD and the next trade automatically becomes real.

| | |
|---|---|
| Dashboard | http://localhost:5173 |
| REST API + Swagger docs | http://localhost:8000/docs |
| WebSocket | `ws://localhost:8000/ws` |
| SQLite DB | `./data/polybot.db` |

---

## One-command setup

```bash
cp .env.example .env
docker compose up --build
```

- Backend → http://localhost:8000  ·  API docs at `/docs`
- Dashboard → http://localhost:5173

The bot launches in **AUTO mode** with a $5.00 simulated bankroll. Without API keys or a funded wallet it stays paper — zero risk, real Polymarket data.

---

## Manual setup (no Docker)

The fastest path is the bundled dev launcher — it creates the venv, installs Python + npm deps, copies `.env` if needed, and runs backend + frontend together. Stop with Ctrl+C and both children die cleanly.

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
```

**macOS / Linux / git-bash / WSL:**

```bash
bash scripts/dev.sh
```

Open http://localhost:5173.

### Or the long way, step by step

**Backend:**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn backend.main:app --reload --port 8000
```

**Frontend:**

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173.

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                    REACT DASHBOARD                          │
│  Header · BankrollChart · MarketTable · TradeLog ·         │
│  StatsPanel · AlertsFeed · ModeToggle · ConfigPanel        │
└──────────────────────────┬─────────────────────────────────┘
                           │  REST + WebSocket
┌──────────────────────────▼─────────────────────────────────┐
│                    FASTAPI BACKEND                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  scanner.py  │→ │ edge_calc.py │→ │     kelly.py     │  │
│  └──────────────┘  └──────────────┘  └────────┬─────────┘  │
│         ▲                                      │            │
│         │                                      ▼            │
│  ┌──────┴───────┐  ┌──────────────┐   ┌────────────────┐   │
│  │ scheduler.py │  │  wallet.py   │──▶│  executor.py   │   │
│  │ (APScheduler)│  │ (Polygon RO) │   │ paper or live  │   │
│  └──────────────┘  └──────────────┘   └────────┬───────┘   │
│         ▲                                      │            │
│         │                                      ▼            │
│  ┌──────┴────────────────────────────────────────────────┐  │
│  │      portfolio.py  ·  db.py (SQLModel + SQLite)       │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────┬─────────────────────────────────┘
                           ▼
              Polymarket CLOB V2 · Polygon (pUSD) · Metaculus · Kalshi
```

---

## How it makes money

### 1. Market scanner (`backend/scanner.py`)
Hits the Polymarket Gamma REST API every 60 seconds. Filters by:
- `liquidity > $5,000`
- `volume_24h > $1,000`
- `time_to_close > 6h`
- `spread < 10%`

### 2. Edge calculator (`backend/edge_calc.py`)
Builds a **model probability** by blending external oracles in **log-odds space**:
- **Metaculus** — community forecasting
- **Kalshi** — regulated US exchange
- **PredictIt** — political markets

When no oracle matches, falls back to a conservative spread-aware heuristic that pulls thin markets toward 0.50 (don't fabricate edge).

`edge = model_probability − market_implied_probability`
Markets with `|edge| > 3%` (configurable) and `confidence > 0.65` enter the candidate set, ranked by `edge × liquidity_score × time_weight × confidence`.

### 3. Kelly Criterion sizer (`backend/kelly.py`)
For a binary YES contract priced at `p` with model win prob `m`:

```
f* = (m / p) − 1       # full Kelly fraction
```

Applied with **fractional Kelly** for survival:
- Stage-aware multiplier (full → 3/4 → 1/2 → 1/4 as bankroll grows)
- Confidence-weighted (low confidence shrinks the stake linearly)
- Hard-capped at **20% of bankroll** per position
- Hard floor at **$0.50** (Polymarket minimum)
- Rejects if any stage / global gate fails

### 4. Executor (`backend/executor.py`)
- **Paper mode**: simulated fills at mid-price + synthetic adverse slippage
- **Live mode**: posts CLOB limit orders via `py-clob-client`, polls fills, cancels stale orders after 5 min, **rejects** fills that deviate > 2% from expected price

### 5. Portfolio manager (`backend/portfolio.py`)
- Tracks cash, open positions, realized/unrealized PnL
- Detects bankroll stage transitions and emits alerts
- Enforces **per-trade stop-loss** at -80% of position value
- **Circuit breaker**: halts all trading on 30% drawdown from peak

---

## Compounding stages

The bot auto-shifts gears based on the current bankroll:

| Stage | Bankroll        | Kelly | Min Edge | Max Positions | Posture        |
|-------|-----------------|-------|----------|---------------|----------------|
| 1     | $0 → $25        | 1.00× | 8.0%     | 1             | Ignition       |
| 2     | $25 → $100      | 0.75× | 5.0%     | 2             | Acceleration   |
| 3     | $100 → $500     | 0.50× | 3.5%     | 3             | Cruise         |
| 4     | $500+           | 0.25× | 3.0%     | 3             | Preservation   |

Every transition is logged and surfaced in the alerts feed.

---

## Trading modes — paper, live, and auto

The bot has **three configured modes** (set via `TRADING_MODE` in `.env`):

| Mode    | Behaviour                                                                                                                                       |
|---------|--------------------------------------------------------------------------------------------------------------------------------------------------|
| `paper` | **Always simulated.** Real Polymarket data, fake fills, zero risk. No wallet needed.                                                            |
| `live`  | **Always real.** Every trade goes through the CLOB. Requires wallet + API keys, plus an explicit dashboard confirmation.                        |
| `auto`  | **The default.** Bot trades real USDC when the on-chain wallet has ≥ `AUTO_MODE_MIN_BALANCE_USD` ($5 default). Below that, it paper-trades into a **separate ledger** that keeps compounding so you can see how it would have performed. |

Auto mode is the killer feature: drop the project, run `docker compose up`, and the bot paper-trades immediately. Send $5+ of USDC to the wallet whose private key is in `.env`, and the **next trade automatically becomes real**. No mode flips, no restarts, no manual intervention. When the wallet dips below the threshold (e.g., a stop-loss takes you out), it reverts to paper.

| Aspect              | PAPER                       | LIVE                                            |
|---------------------|-----------------------------|-------------------------------------------------|
| Wallet required     | No                          | Yes (Polygon, USDC.e)                           |
| API keys            | No                          | Yes (Polymarket CLOB)                           |
| Real money          | No — simulated              | Yes — real USDC                                 |
| Order routing       | In-process simulation       | `py-clob-client` → Polymarket CLOB              |
| Fill source         | Mid-price + 25 bps          | Real fills, slippage guard ±2%                  |
| DB rows             | `Trade.mode = PAPER`        | `Trade.mode = LIVE`                             |
| Bankroll source     | `INITIAL_BANKROLL` + PnL    | On-chain USDC balance + open-position value     |
| UI watermark        | "PAPER" (amber)             | "LIVE ●" (red, pulsing)                         |
| In `auto` mode      | When wallet < threshold     | When wallet ≥ threshold                         |

The wallet is polled every 30 seconds. Mode transitions ("AUTO → LIVE" when funded, "AUTO → PAPER" when drained) appear immediately in the Alerts Feed.

---

## Generating Polymarket API keys (for live + auto modes)

To unlock live trading, you need **four** values in `.env`:

| Variable                 | Source                                                |
|--------------------------|--------------------------------------------------------|
| `PRIVATE_KEY`            | A Polygon wallet you control                           |
| `POLYMARKET_API_KEY`     | Generated by Polymarket's CLOB (one-time, per wallet)  |
| `POLYMARKET_SECRET`      | ditto                                                  |
| `POLYMARKET_PASSPHRASE`  | ditto                                                  |

The three Polymarket creds aren't something you create on the Polymarket website — they're **derived from a signature** that your wallet produces. The bot ships with a helper script that does the whole flow in one command.

### One-command key generation

```bash
python scripts/generate_polymarket_keys.py
```

What it does:

1. Reads `PRIVATE_KEY` from your `.env` (or prompts you for it — input is hidden)
2. Derives your Polygon wallet address from the key and shows it
3. Connects to the Polymarket CLOB V2
4. Signs an EIP-712 challenge locally — **your private key never leaves the machine**
5. Asks Polymarket for your API credentials (or derives the ones that already exist if you've generated them before — the operation is idempotent)
6. Prints the three credentials, ready to paste
7. Offers to write them straight into `.env` for you

To skip the interactive prompt and write directly:

```bash
python scripts/generate_polymarket_keys.py --write
```

To target the Amoy testnet instead of Polygon mainnet:

```bash
python scripts/generate_polymarket_keys.py --chain-id 80002 --host https://clob-staging.polymarket.com
```

### "I don't have a Polygon wallet — where do I get a private key?"

A private key is just a 64-character hex string that controls a Polygon address. You have three options:

**Option A — let the script create one for you (recommended for the bot):**

```bash
python scripts/generate_polymarket_keys.py --new-wallet
```

This generates a brand-new wallet using your OS's cryptographic randomness, prints the address + private key, makes you type `I SAVED IT` to confirm you backed it up, and then continues straight into the key-generation flow. The wallet has never touched a browser or any other machine — perfect for a dedicated bot.

**Option B — MetaMask** (good if you want a browser UI too):
1. Install [MetaMask](https://metamask.io/) browser extension
2. Create a new account (write down the seed phrase!)
3. Polygon mainnet is included by default — switch to it
4. Click the three dots → **Account details** → **Show private key** → enter your password → copy the key
5. Paste the key when the script prompts you

**Option C — any other wallet** (Rabby, Trust Wallet, Coinbase Wallet, hardware wallet via export, etc.): any wallet that lets you export a raw private key works. The key is the same regardless of which wallet UI created it.

> **Don't reuse your main wallet.** If the bot ever has a bug, the worst case is the funds in this one wallet. Use a fresh address dedicated to the bot.

### Step-by-step from zero

> **April 2026 migration note**: Polymarket replaced **USDC.e** with **pUSD** (Polymarket USD) as its collateral token on April 28, 2026. All trades settle in pUSD, which is a 1:1 wrapper around USDC. You can deposit either native USDC or USDC.e — they both get wrapped into pUSD. The bot's `auto`-mode threshold check looks at your **pUSD** balance, not USDC.e.

1. **Create a dedicated Polygon wallet** using one of the three options above. Save the private key somewhere safe (password manager).
2. **Run the key script** — it'll generate Polymarket API credentials and write them to `.env`.
   ```bash
   python scripts/generate_polymarket_keys.py --write
   ```
3. **Fund the wallet — two paths.**

   **Path A: through polymarket.com (easiest for non-developers)**
   - Visit [polymarket.com](https://polymarket.com), connect this wallet, accept the terms of service
   - Click **Deposit**. Polymarket will give you a bridge address that accepts USDC from many chains (Ethereum, Arbitrum, Base, Solana, etc.) and auto-wraps to pUSD
   - The Polymarket UI handles MATIC gas internally on first deposit
   - Wait ~1–5 minutes for the deposit to clear → it shows up as pUSD in your wallet
   - **Important: still send a small amount of MATIC (~0.1) directly to the wallet** — the bot itself needs gas to place orders, and Polymarket's deposit doesn't fund this

   **Path B: direct on-chain (cheaper, more control)**
   - From an exchange that supports Polygon withdrawals (Coinbase, Binance, Kraken, OKX, KuCoin, Bybit, etc.):
     - Withdraw **USDC** selecting **"Polygon"** as the network → arrives as either native USDC or USDC.e depending on the exchange
     - Withdraw **0.5+ MATIC** selecting **"Polygon"** as the network
   - If your USDC arrived as **USDC.e** (the bot tells you in the wallet badge — "⚠ X.XX USDC.e needs wrap"), wrap it:
     ```bash
     python scripts/wrap_usdc.py
     ```
     This calls Polymarket's CollateralOnramp contract (one approve + one wrap transaction, costs <$0.05 in gas).
   - If your USDC arrived as **native USDC**: visit polymarket.com once to trigger the auto-wrap, or use Polymarket's deposit endpoint.

4. **Visit polymarket.com with this wallet at least once** and accept the terms of service. Polymarket's CLOB will reject API orders from wallets that haven't agreed to the TOS.

5. **Restart the bot.** Settings are cached at startup, so `.env` changes only take effect on restart.
   ```bash
   # Ctrl+C the dev.sh terminal, then:
   bash scripts/dev.sh
   ```

6. **Watch the wallet badge.** Within 30 seconds the dashboard header should show:
   - `wallet: $X.XX pUSD` in green if pUSD ≥ `AUTO_MODE_MIN_BALANCE_USD` (defaults to $5) → bot will trade LIVE
   - `wallet: $X.XX pUSD` in amber if pUSD is below the threshold → bot stays in PAPER
   - A second amber pill `⚠ $X.XX USDC.e needs wrap` if you funded with USDC.e and haven't wrapped yet

### What if I don't want a script?

You can do it manually with a Python REPL — `scripts/generate_polymarket_keys.py` is just a friendlier wrapper around this:

```python
from py_clob_client_v2 import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    chain_id=137,
    key="0xYOUR_PRIVATE_KEY",
)
creds = client.create_or_derive_api_key()
print(creds.api_key, creds.api_secret, creds.api_passphrase)
```

The `create_or_derive_api_key()` call is **idempotent** — call it twice and you get the same credentials. There's no risk of "burning" keys by running the script multiple times.

### Safety notes

- The script reads your private key locally and signs the challenge **on your machine**. Only the signed challenge (not the key) goes to Polymarket.
- The script never logs or transmits the key anywhere.
- Use a **dedicated** wallet. If the bot has a bug, you only lose what's in this wallet — not your main holdings.
- The `.env` file is git-ignored. **Never** commit it.

---

## Configuration

All knobs live in `.env`. Highlights:

```bash
TRADING_MODE=auto                 # paper | live | auto (default = auto)
AUTO_MODE_MIN_BALANCE_USD=5.00    # wallet USDC threshold to go live in auto mode

INITIAL_BANKROLL=5.00             # starting paper bankroll
KELLY_FRACTION=0.25               # global multiplier
MIN_EDGE_THRESHOLD=0.03           # 3%
MIN_CONFIDENCE=0.65
MAX_CONCURRENT_BETS=3
MAX_POSITION_FRACTION=0.20        # 20% bankroll per position
STOP_LOSS_FRACTION=0.80           # close at -80% of position value
DRAWDOWN_CIRCUIT_BREAKER=0.30     # halt all trading at -30% peak DD

POLYMARKET_API_KEY=               # live + auto modes
POLYMARKET_SECRET=
POLYMARKET_PASSPHRASE=
PRIVATE_KEY=                      # Polygon wallet — also used to read USDC balance
```

A subset of these (Kelly fraction, min edge) is also adjustable **at runtime** from the dashboard's Config Panel without restarting.

---

## Dashboard tour

The dashboard is built for a developer's eye — dark `#0d1117` background, JetBrains Mono everywhere, terminal-style status pills.

- **Header** — bankroll, drawdown, current stage, mode + WS status, scan/pause buttons
- **Bankroll Chart** — live area chart, $5 → current, with $25/$100/$500/$1000 reference lines
- **Stats Panel** — win rate, ROI, average edge, Sharpe, biggest win/loss, realized + unrealized PnL
- **Active Positions** — live table with mark-to-market PnL
- **Alerts Feed** — color-coded events (opportunities, fills, stop-losses, stage transitions, errors)
- **Trade Log** — full audit trail with outcome badges
- **Mode Toggle** — PAPER ↔ LIVE with confirmation gate
- **Config Panel** — adjust Kelly + edge thresholds live

---

## Testing

```bash
pytest -q
# 59 passed in 11.71s
```

Tests live in `backend/tests/`:

| File                       | Tests | Covers                                                                                 |
|----------------------------|-------|----------------------------------------------------------------------------------------|
| `test_kelly.py`            | 18    | Kelly formula correctness, drawdown math, stage gating, position caps, min-bet floor    |
| `test_auto_mode.py`        | 11    | `effective_mode()` routing, wallet thresholds, USDC.e-ignored, paper fallback           |
| `test_edge_calc.py`        | 10    | Tokenization, geometric oracle blending, side-flipping, zero-edge no-oracle invariant   |
| `test_scheduler.py`        | 8     | Job registration, scan immediacy, one-shot "all oracles down" + recovery alerts         |
| `test_executor.py`         | 6     | Paper fill → position → mark-to-market → close → realized PnL                           |
| `test_api_serialization.py`| 6     | `inf`/`NaN` coercion, stage serialization, `/config` `/state` `/stats` endpoint smoke   |

Each test gets a fresh SQLModel schema via the `fresh_db` fixture, and `get_settings()` is cache-cleared between tests by an autouse fixture in `conftest.py`.

> 🎯 **War story**: The first version of `kelly.py` shipped with the wrong closed-form simplification (`f = m/p − 1`, which is exactly **50% too aggressive** at typical Polymarket prices). The textbook-equivalence test in `test_kelly_matches_textbook_formula` caught it before any capital touched a CLOB. The correct form is `(m − p) / (1 − p)`. This is exactly what unit tests are for.

---

## Safety notes

- **Paper-trade for at least a week** before flipping to live. Verify your model probabilities are calibrated.
- **Live mode commits real USDC**. The slippage guard, stop-loss, and circuit breaker exist for a reason — leave them on.
- **Polymarket terms of service**: trading bots are permitted, but US-based users should review compliance.
- **Private key handling**: `.env` is gitignored. Never commit your key. Use a dedicated wallet — not your main one.
- **Gas on Polygon**: low but non-zero. The bot estimates gas before each order.

---

## File structure

```
polymarket-trading-bot/
├── backend/
│   ├── __init__.py
│   ├── main.py              # FastAPI app: REST routes, WebSocket, lifespan
│   ├── scanner.py           # Polymarket Gamma API → MarketSnapshot → filter → rank
│   ├── edge_calc.py         # Metaculus / Kalshi oracles + log-odds blending
│   ├── kelly.py             # Kelly Criterion + stage-aware sizing gates
│   ├── executor.py          # Paper + live (py-clob-client-v2) execution
│   ├── portfolio.py         # Dual bankroll, PnL, drawdown, effective_mode()
│   ├── wallet.py            # Read-only Polygon pUSD/USDC.e/MATIC balance polling
│   ├── db.py                # SQLModel schema + KV helpers
│   ├── config.py            # Pydantic Settings + STAGES table
│   ├── scheduler.py         # APScheduler jobs + WebSocket broadcaster seam
│   ├── Dockerfile
│   └── tests/
│       ├── conftest.py
│       ├── test_kelly.py
│       ├── test_edge_calc.py
│       ├── test_executor.py
│       ├── test_auto_mode.py
│       ├── test_scheduler.py
│       └── test_api_serialization.py
├── frontend/
│   ├── src/
│   │   ├── App.jsx
│   │   ├── main.jsx
│   │   ├── index.css
│   │   ├── components/
│   │   │   ├── Header.jsx
│   │   │   ├── BankrollChart.jsx
│   │   │   ├── MarketTable.jsx
│   │   │   ├── TradeLog.jsx
│   │   │   ├── ModeToggle.jsx
│   │   │   ├── StatsPanel.jsx
│   │   │   ├── AlertsFeed.jsx
│   │   │   └── ConfigPanel.jsx
│   │   ├── hooks/useWebSocket.js
│   │   └── lib/{api.js, format.js}
│   ├── public/favicon.svg
│   ├── package.json
│   ├── eslint.config.js
│   ├── postcss.config.js
│   ├── tailwind.config.js
│   ├── vite.config.js
│   └── Dockerfile
├── scripts/
│   ├── dev.sh                       # one-shot launcher (macOS / Linux / git-bash / WSL)
│   ├── dev.ps1                      # one-shot launcher (Windows PowerShell)
│   ├── generate_polymarket_keys.py  # derive Polymarket API creds from your wallet
│   └── wrap_usdc.py                 # wrap USDC.e → pUSD via Polymarket's CollateralOnramp
├── data/                            # SQLite DB lives here (gitignored)
├── .env.example
├── .gitignore
├── docker-compose.yml
├── pytest.ini
├── requirements.txt
├── CONTEXT.md                       # operating manual for AI agents
└── README.md
```

---

## Contributing / extending

Before you (or your AI agent) start coding, read [`CONTEXT.md`](./CONTEXT.md). It documents:
- The hard invariants (the Kelly formula, the auto-mode paper-fallback, the no-fabricated-edge rule, the secrets policy)
- Coding conventions for both Python and React
- How to add a new probability oracle, a new dashboard panel, or a new runtime setting
- A list of things the agent is **not** allowed to do without explicit user permission

Issues and pull requests are welcome at [github.com/manishhansal/polymarket-trading-bot](https://github.com/manishhansal/polymarket-trading-bot). Please run `pytest -q` (all 59 green) and `npm run lint` before opening a PR.

Common shortcuts:

```bash
# Run the full test suite
pytest -q

# Force an immediate market scan (skip the 60s cron)
curl -X POST http://localhost:8000/api/scan

# Pause / resume trading globally
curl -X POST http://localhost:8000/api/pause \
  -H 'content-type: application/json' -d '{"paused": true}'

# Inspect the SQLite DB
sqlite3 data/polybot.db "select created_at, status, side, size_usd, edge, pnl_usd from trade order by id desc limit 20;"
```

---

## License

MIT. Trade at your own risk. The author is not your financial advisor.
