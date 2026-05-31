"""Central configuration loaded from environment variables.

All tunable knobs live here. Secrets are read from `.env` via pydantic-settings
and are *never* hardcoded. Importing this module is side-effect free.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    """Strongly-typed runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Trading mode ---
    # "paper" — always simulated (zero risk, no wallet needed)
    # "live"  — always real money on Polygon (requires wallet + API keys)
    # "auto"  — switch automatically:
    #     wallet USDC ≥ auto_mode_min_balance_usd → LIVE
    #     wallet USDC <  auto_mode_min_balance_usd → PAPER (with paper ledger)
    trading_mode: Literal["paper", "live", "auto"] = "auto"
    auto_mode_min_balance_usd: float = 5.00

    # --- Bankroll & risk ---
    initial_bankroll: float = 5.00
    max_concurrent_bets: int = 3
    kelly_fraction: float = 0.25
    min_edge_threshold: float = 0.03
    min_confidence: float = 0.65
    max_position_fraction: float = 0.20
    stop_loss_fraction: float = 0.80
    drawdown_circuit_breaker: float = 0.30
    min_bet_size_usd: float = 0.50

    # --- Market filters ---
    min_liquidity_usd: float = 5_000.0
    min_volume_24h_usd: float = 1_000.0
    min_hours_to_close: float = 6.0

    # --- Polymarket / Polygon ---
    polymarket_api_key: str = ""
    polymarket_secret: str = ""
    polymarket_passphrase: str = ""
    private_key: str = ""
    polygon_rpc_url: str = "https://polygon-rpc.com"
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    chain_id: int = 137

    # --- Probability oracles ---
    # Metaculus closed unauthenticated access in 2025. Set METACULUS_TOKEN
    # in .env to enable; otherwise this source is silently skipped.
    metaculus_api: str = "https://www.metaculus.com/api"
    metaculus_token: str = ""
    # Kalshi public endpoint — works without auth. Despite the "elections"
    # subdomain, it serves every Kalshi market (sports, econ, climate, ...).
    kalshi_api: str = "https://api.elections.kalshi.com/trade-api/v2"

    # --- Scheduler ---
    scan_interval_seconds: int = 60
    order_timeout_seconds: int = 300
    websocket_heartbeat_seconds: int = 5

    # --- Database ---
    database_url: str = f"sqlite:///{DATA_DIR / 'polybot.db'}"

    # --- API server ---
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    @field_validator("kelly_fraction", "min_edge_threshold", "min_confidence",
                     "max_position_fraction", "stop_loss_fraction",
                     "drawdown_circuit_breaker")
    @classmethod
    def _fraction_in_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"Fraction must be in [0, 1], got {v}")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_live(self) -> bool:
        """True when the configured mode is the LIVE override (not auto)."""
        return self.trading_mode == "live"

    @property
    def is_auto(self) -> bool:
        return self.trading_mode == "auto"

    @property
    def has_live_credentials(self) -> bool:
        return bool(
            self.polymarket_api_key
            and self.polymarket_secret
            and self.polymarket_passphrase
            and self.private_key
        )

    @property
    def has_wallet_key(self) -> bool:
        """Just the private key — enough to *read* the wallet balance."""
        return bool(self.private_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton."""
    return Settings()


# ============================================================
# COMPOUNDING STAGES — $5 → $1,000 strategy
# ============================================================
# Each stage activates automatically when bankroll crosses the floor.
# Lower bankroll = more aggressive Kelly + stricter edge filter.

STAGES = [
    {
        "name": "Stage 1 — Ignition",
        "floor": 0.0,
        "ceiling": 25.0,
        "kelly_fraction": 1.00,
        "min_edge": 0.08,
        "max_positions": 1,
        "description": "$5 → $25: full Kelly, only high-edge plays",
    },
    {
        "name": "Stage 2 — Acceleration",
        "floor": 25.0,
        "ceiling": 100.0,
        "kelly_fraction": 0.75,
        "min_edge": 0.05,
        "max_positions": 2,
        "description": "$25 → $100: 3/4 Kelly, edge > 5%",
    },
    {
        "name": "Stage 3 — Cruise",
        "floor": 100.0,
        "ceiling": 500.0,
        "kelly_fraction": 0.50,
        "min_edge": 0.035,
        "max_positions": 3,
        "description": "$100 → $500: half Kelly, edge > 3.5%",
    },
    {
        "name": "Stage 4 — Preservation",
        "floor": 500.0,
        "ceiling": float("inf"),
        "kelly_fraction": 0.25,
        "min_edge": 0.03,
        "max_positions": 3,
        "description": "$500+: quarter Kelly, tightest stops",
    },
]


def get_active_stage(bankroll: float) -> dict:
    """Return the stage config that matches the current bankroll."""
    for stage in STAGES:
        if stage["floor"] <= bankroll < stage["ceiling"]:
            return stage
    return STAGES[-1]
