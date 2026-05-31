"""Shared pytest fixtures — isolated DB per test, no module reloads."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Point the bot at a throwaway DB BEFORE any backend module imports.
_TMP = Path(tempfile.mkdtemp(prefix="polybot_test_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["TRADING_MODE"] = "paper"
os.environ["INITIAL_BANKROLL"] = "5.00"


@pytest.fixture(autouse=True)
def _reset_settings():
    """Rebuild the settings singleton each test so env tweaks take effect."""
    from backend.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fresh_db():
    """Drop + recreate all tables. Returns the `backend.db` module."""
    from backend import db as db_module
    from sqlmodel import SQLModel

    SQLModel.metadata.drop_all(db_module.engine)
    SQLModel.metadata.create_all(db_module.engine)
    return db_module
