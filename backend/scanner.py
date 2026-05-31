"""Polymarket market scanner.

Pulls open markets from the Polymarket Gamma REST API, filters by liquidity,
volume, and time-to-close, then ranks the survivors by edge × liquidity ×
time-weight (computed by `edge_calc`).

Designed to be schedule-friendly: one `scan()` call returns a sorted list
of `MarketOpportunity` records. The scheduler decides what to do with them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

from .config import get_settings
from .edge_calc import EdgeResult, compute_edge


@dataclass
class MarketSnapshot:
    """Lightweight Polymarket market record."""

    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    spread: float
    liquidity: float
    volume_24h: float
    end_date: datetime
    hours_to_close: float


@dataclass
class MarketOpportunity:
    """A market that passed all filters, paired with its edge result."""

    market: MarketSnapshot
    edge: EdgeResult


@dataclass
class ScanResult:
    """Per-scan summary: the qualified opportunities plus diagnostics.

    Diagnostic counts are essential because when EVERY market is oracleless,
    `opportunities` is empty (edge=0 fails the min_edge_threshold gate). Without
    the counts, the scheduler cannot distinguish "we found nothing because
    everything was fairly priced" from "we found nothing because no oracle
    matched any market" — and the user sees total silence in both cases.
    """

    opportunities: list["MarketOpportunity"]
    evaluated_count: int      # markets that survived basic filters and reached compute_edge
    oracleless_count: int     # of those, how many returned sources=["no-oracle"]


# ============================================================
# HTTP
# ============================================================


async def _fetch_open_markets(client: httpx.AsyncClient, limit: int = 200) -> list[dict]:
    """Pull a page of open markets from the Gamma API."""
    settings = get_settings()
    url = f"{settings.gamma_host}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": limit,
        "order": "volume24hr",
        "ascending": "false",
    }
    try:
        r = await client.get(url, params=params, timeout=15.0)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("data", []) or []
    except Exception as e:
        logger.warning(f"Gamma API fetch failed: {e}")
        return []


# ============================================================
# PARSING
# ============================================================


def _parse_market(raw: dict) -> Optional[MarketSnapshot]:
    """Convert a raw Gamma payload into a typed snapshot, or None if invalid."""
    try:
        market_id = str(raw.get("id") or raw.get("conditionId") or "")
        condition_id = str(raw.get("conditionId") or market_id)
        question = raw.get("question") or raw.get("title") or ""
        if not market_id or not question:
            return None

        # Token ids come as a JSON-encoded string list "[\"yes\", \"no\"]".
        tokens_field = raw.get("clobTokenIds") or raw.get("tokenIds") or "[]"
        if isinstance(tokens_field, str):
            import json
            try:
                token_ids = json.loads(tokens_field)
            except json.JSONDecodeError:
                token_ids = []
        else:
            token_ids = tokens_field or []
        if len(token_ids) < 2:
            return None
        yes_token, no_token = str(token_ids[0]), str(token_ids[1])

        # Outcome prices arrive as JSON-encoded string list too.
        prices_field = raw.get("outcomePrices") or "[]"
        if isinstance(prices_field, str):
            import json
            try:
                prices = [float(p) for p in json.loads(prices_field)]
            except (json.JSONDecodeError, ValueError):
                prices = []
        else:
            prices = [float(p) for p in (prices_field or [])]
        if len(prices) < 2:
            return None
        yes_price, no_price = prices[0], prices[1]
        if not (0.0 < yes_price < 1.0):
            return None
        spread = abs(1.0 - (yes_price + no_price))

        liquidity = float(raw.get("liquidity") or raw.get("liquidityNum") or 0.0)
        volume_24h = float(raw.get("volume24hr") or raw.get("volume24hrNum") or 0.0)

        end_iso = raw.get("endDate") or raw.get("end_date_iso") or raw.get("endDateIso")
        if not end_iso:
            return None
        end_date = datetime.fromisoformat(str(end_iso).replace("Z", "+00:00"))
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        hours_to_close = (end_date - datetime.now(timezone.utc)).total_seconds() / 3600.0

        return MarketSnapshot(
            market_id=market_id,
            condition_id=condition_id,
            question=question,
            yes_token_id=yes_token,
            no_token_id=no_token,
            yes_price=yes_price,
            no_price=no_price,
            spread=spread,
            liquidity=liquidity,
            volume_24h=volume_24h,
            end_date=end_date,
            hours_to_close=hours_to_close,
        )
    except Exception as e:
        logger.debug(f"Failed to parse market: {e}")
        return None


def _passes_filters(m: MarketSnapshot) -> bool:
    s = get_settings()
    return (
        m.liquidity >= s.min_liquidity_usd
        and m.volume_24h >= s.min_volume_24h_usd
        and m.hours_to_close >= s.min_hours_to_close
        and m.spread < 0.10  # Reject illiquid books with wide spreads outright
    )


# ============================================================
# PUBLIC API
# ============================================================


async def scan() -> ScanResult:
    """Pull markets, filter, compute edge, return ranked opportunities + diagnostics."""
    settings = get_settings()
    opportunities: list[MarketOpportunity] = []
    evaluated = 0
    oracleless = 0

    async with httpx.AsyncClient(headers={"User-Agent": "polybot/0.1"}) as client:
        raw_markets = await _fetch_open_markets(client)
        snapshots = [m for m in (_parse_market(r) for r in raw_markets) if m is not None]
        filtered = [m for m in snapshots if _passes_filters(m)]

        for m in filtered:
            try:
                edge = await compute_edge(
                    market_id=m.market_id,
                    question=m.question,
                    yes_price=m.yes_price,
                    spread=m.spread,
                    volume_24h=m.volume_24h,
                    liquidity=m.liquidity,
                    hours_to_close=m.hours_to_close,
                    client=client,
                )
                evaluated += 1
                if edge.sources == ["no-oracle"]:
                    oracleless += 1
                if edge.abs_edge >= settings.min_edge_threshold:
                    opportunities.append(MarketOpportunity(market=m, edge=edge))
            except Exception as e:
                logger.debug(f"Edge calc failed for {m.market_id}: {e}")

        logger.info(
            f"Scanner: fetched={len(raw_markets)} parsed={len(snapshots)} "
            f"filtered={len(filtered)} evaluated={evaluated} "
            f"oracleless={oracleless} qualified={len(opportunities)}"
        )

    opportunities.sort(key=lambda o: o.edge.score, reverse=True)
    return ScanResult(
        opportunities=opportunities,
        evaluated_count=evaluated,
        oracleless_count=oracleless,
    )
