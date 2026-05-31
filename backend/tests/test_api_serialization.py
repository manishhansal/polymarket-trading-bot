"""Regression tests for JSON-serializability of API responses.

Why this exists: Stage 4 of STAGES uses `float('inf')` as its ceiling, which
Python's vanilla `json.dumps` rejects ("Out of range float values are not JSON
compliant: inf"). The /api/config endpoint must coerce those to None.
"""

from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient

from backend.config import STAGES
from backend.main import _json_safe_number, _serialize_stage, app


# ============================================================
# UNIT
# ============================================================


def test_json_safe_number_passes_normal_values():
    assert _json_safe_number(3.14) == 3.14
    assert _json_safe_number(0) == 0
    assert _json_safe_number(-2.0) == -2.0
    assert _json_safe_number("not a number") == "not a number"
    assert _json_safe_number(None) is None


def test_json_safe_number_converts_inf_and_nan():
    assert _json_safe_number(float("inf")) is None
    assert _json_safe_number(float("-inf")) is None
    assert _json_safe_number(float("nan")) is None


def test_serialize_stage_is_strict_json_safe():
    """Every stage in STAGES must round-trip through json.dumps without errors."""
    for raw in STAGES:
        s = _serialize_stage(raw)
        encoded = json.dumps(s, allow_nan=False)
        decoded = json.loads(encoded)
        assert decoded["name"] == raw["name"]
        if math.isinf(raw["ceiling"]):
            assert decoded["ceiling"] is None
        else:
            assert decoded["ceiling"] == raw["ceiling"]


# ============================================================
# INTEGRATION — hit the live endpoint with the FastAPI TestClient
# ============================================================


@pytest.fixture
def client(fresh_db):
    """FastAPI test client backed by the per-test SQLite db."""
    from backend.db import init_db
    init_db()
    return TestClient(app)


def test_config_endpoint_serializes_without_inf(client):
    r = client.get("/api/config")
    assert r.status_code == 200, r.text

    body = r.json()
    assert "stages" in body
    assert "active_stage" in body
    assert len(body["stages"]) == 4

    last = body["stages"][-1]
    assert last["name"].startswith("Stage 4")
    assert last["ceiling"] is None  # was float('inf')

    # Confirm the full payload survives a strict JSON round-trip (no NaN/inf).
    json.dumps(body, allow_nan=False)


def test_state_endpoint_ok(client):
    r = client.get("/api/state")
    assert r.status_code == 200
    json.dumps(r.json(), allow_nan=False)


def test_stats_endpoint_ok(client):
    r = client.get("/api/stats")
    assert r.status_code == 200
    json.dumps(r.json(), allow_nan=False)
