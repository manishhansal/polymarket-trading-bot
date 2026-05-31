"""SQLite + SQLModel persistence layer.

Tables:
    - trades         : every paper or live trade attempt
    - positions      : currently open positions
    - bankroll_snap  : bankroll time series for the dashboard chart
    - alerts         : mispricing / system / fill events
    - stage_events   : compounding-stage transitions
    - settings_kv    : runtime-mutable settings (mode toggle, kelly fraction, ...)

The schema is intentionally PostgreSQL-compatible: only standard column types
and indexes — no SQLite-specific affinity tricks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Iterator, Optional

from sqlmodel import Field, Session, SQLModel, create_engine, select

from .config import get_settings


# ============================================================
# ENUMS
# ============================================================


class TradeSide(str, Enum):
    YES = "YES"
    NO = "NO"


class TradeStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    RESOLVED_WIN = "resolved_win"
    RESOLVED_LOSS = "resolved_loss"
    STOPPED_OUT = "stopped_out"
    REJECTED = "rejected"


class TradeMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class AlertLevel(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    OPPORTUNITY = "opportunity"


# ============================================================
# MODELS
# ============================================================


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Trade(SQLModel, table=True):
    """A trade attempt — paper or live."""

    id: Optional[int] = Field(default=None, primary_key=True)
    mode: TradeMode = Field(index=True)
    status: TradeStatus = Field(default=TradeStatus.PENDING, index=True)

    market_id: str = Field(index=True)
    market_question: str
    token_id: str
    side: TradeSide

    entry_price: float
    fill_price: Optional[float] = None
    size_shares: float
    size_usd: float

    model_probability: float
    edge: float
    confidence: float
    kelly_fraction_used: float
    stage_name: str

    pnl_usd: float = 0.0
    pnl_pct: float = 0.0

    order_id: Optional[str] = Field(default=None, index=True)
    tx_hash: Optional[str] = None
    error: Optional[str] = None

    created_at: datetime = Field(default_factory=_utcnow, index=True)
    filled_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None


class Position(SQLModel, table=True):
    """Currently-open position. Closed positions remain only in `trades`."""

    id: Optional[int] = Field(default=None, primary_key=True)
    trade_id: int = Field(foreign_key="trade.id", index=True)
    market_id: str = Field(index=True)
    token_id: str
    side: TradeSide
    size_shares: float
    avg_price: float
    cost_basis_usd: float
    current_price: float = 0.0
    unrealized_pnl_usd: float = 0.0
    opened_at: datetime = Field(default_factory=_utcnow)


class BankrollSnapshot(SQLModel, table=True):
    """Time-series bankroll for the live chart."""

    id: Optional[int] = Field(default=None, primary_key=True)
    bankroll: float
    cash: float
    open_position_value: float
    realized_pnl: float
    unrealized_pnl: float
    mode: TradeMode
    stage_name: str
    timestamp: datetime = Field(default_factory=_utcnow, index=True)


class Alert(SQLModel, table=True):
    """Mispricing alerts, fills, errors, stage transitions."""

    id: Optional[int] = Field(default=None, primary_key=True)
    level: AlertLevel = Field(index=True)
    title: str
    message: str
    market_id: Optional[str] = Field(default=None, index=True)
    payload_json: Optional[str] = None
    timestamp: datetime = Field(default_factory=_utcnow, index=True)


class StageEvent(SQLModel, table=True):
    """Bankroll stage transitions — useful for the journey timeline."""

    id: Optional[int] = Field(default=None, primary_key=True)
    from_stage: str
    to_stage: str
    bankroll: float
    timestamp: datetime = Field(default_factory=_utcnow)


class SettingsKV(SQLModel, table=True):
    """Mutable settings that the dashboard can change at runtime."""

    key: str = Field(primary_key=True)
    value: str
    updated_at: datetime = Field(default_factory=_utcnow)


# ============================================================
# ENGINE + SESSION
# ============================================================


_settings = get_settings()
_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, echo=False, connect_args=_connect_args)


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency-style session generator."""
    with Session(engine) as session:
        yield session


def session_scope() -> Session:
    """Plain session for non-FastAPI callers (scheduler jobs, etc.)."""
    return Session(engine)


# ============================================================
# CONVENIENCE QUERIES
# ============================================================


def get_kv(key: str, default: Optional[str] = None) -> Optional[str]:
    with session_scope() as s:
        row = s.get(SettingsKV, key)
        return row.value if row else default


def set_kv(key: str, value: str) -> None:
    with session_scope() as s:
        row = s.get(SettingsKV, key)
        if row:
            row.value = value
            row.updated_at = _utcnow()
        else:
            row = SettingsKV(key=key, value=value)
        s.add(row)
        s.commit()


def latest_bankroll(default: float) -> float:
    with session_scope() as s:
        stmt = select(BankrollSnapshot).order_by(BankrollSnapshot.timestamp.desc()).limit(1)
        snap = s.exec(stmt).first()
        return snap.bankroll if snap else default


def peak_bankroll(default: float) -> float:
    with session_scope() as s:
        stmt = select(BankrollSnapshot)
        snaps = s.exec(stmt).all()
        if not snaps:
            return default
        return max(s.bankroll for s in snaps)
