"""On-chain wallet balance polling (pUSD / USDC.e / MATIC on Polygon).

Polymarket migrated its collateral token from **USDC.e** to **pUSD** on
April 28, 2026. All CLOB V2 orders now reference pUSD, so the bot only
considers `pusd_balance` when deciding whether auto-mode goes LIVE.

USDC.e is still surfaced as `usdce_balance` because it's the input asset
to the CollateralOnramp. If a user funds with USDC.e but hasn't wrapped
yet, the dashboard shows a "needs wrap" hint and `scripts/wrap_usdc.py`
performs the wrap with a single command.

This module is the **only** read-only entry point to the live wallet. We
deliberately *never* expose signing here — that responsibility lives in
`executor.py` and stays there.

Balances are cached for `WALLET_CACHE_SECONDS` to avoid hammering the RPC
every scheduler tick. Use `refresh_balance()` to force a re-fetch.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from loguru import logger

from .config import get_settings


# Canonical Polymarket collateral token (Polygon mainnet) — verified at
# https://docs.polymarket.com/resources/contract-addresses
PUSD_POLYGON_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# Bridged USDC ("USDC.e") on Polygon — the input asset accepted by the
# CollateralOnramp's `wrap()` function.
USDCE_POLYGON_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# CollateralOnramp — wraps USDC.e → pUSD at 1:1. Used by scripts/wrap_usdc.py.
COLLATERAL_ONRAMP_ADDRESS = "0x93070a847efEf7F70739046A929D47a521F5B8ee"

# Both stablecoins use 6 decimals on Polygon.
STABLECOIN_DECIMALS = 6

# Minimal ERC-20 ABI — we only need balanceOf() for read-only polling.
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]

WALLET_CACHE_SECONDS = 30.0


@dataclass
class WalletState:
    """Snapshot of the live Polygon wallet."""

    available: bool          # False = no creds OR RPC down → bot stays paper
    address: Optional[str]   # 0x… checksummed; None if no private key
    pusd_balance: float      # Polymarket USD — what trading actually consumes
    usdce_balance: float     # USDC.e — informational ("you should wrap this")
    matic_balance: float     # Native MATIC for gas
    last_updated: float      # Unix timestamp of the cached value
    error: Optional[str]     # Last error if any, else None

    @property
    def usdc_balance(self) -> float:
        """Back-compat alias. Trading bankroll = pUSD."""
        return self.pusd_balance

    @property
    def needs_wrap(self) -> bool:
        """True if the user has USDC.e sitting around that should be wrapped."""
        return self.usdce_balance > 0.01


# Module-level cache. Cleared on credential change (e.g. after .env edit + restart).
_cache: Optional[WalletState] = None
_cache_lock = asyncio.Lock()


def _unavailable(error: Optional[str] = None) -> WalletState:
    return WalletState(
        available=False,
        address=None,
        pusd_balance=0.0,
        usdce_balance=0.0,
        matic_balance=0.0,
        last_updated=time.time(),
        error=error,
    )


def _derive_address() -> Optional[str]:
    """Best-effort: derive the wallet address from PRIVATE_KEY without RPC."""
    s = get_settings()
    if not s.private_key:
        return None
    try:
        from eth_account import Account
        acct = Account.from_key(s.private_key)
        return acct.address
    except Exception as e:
        logger.debug(f"Address derivation failed: {e}")
        return None


def _fetch_sync() -> WalletState:
    """Blocking RPC call — meant to be wrapped in `asyncio.to_thread`."""
    s = get_settings()
    if not s.private_key:
        return _unavailable("PRIVATE_KEY not set")

    try:
        from web3 import Web3
    except ImportError:
        return _unavailable("web3 package not installed")

    address = _derive_address()
    if address is None:
        return _unavailable("could not derive address from PRIVATE_KEY")

    try:
        w3 = Web3(Web3.HTTPProvider(s.polygon_rpc_url, request_kwargs={"timeout": 8}))
        if not w3.is_connected():
            return _unavailable(f"RPC {s.polygon_rpc_url} not reachable")

        checksum = w3.to_checksum_address(address)

        pusd = w3.eth.contract(
            address=w3.to_checksum_address(PUSD_POLYGON_ADDRESS),
            abi=_ERC20_ABI,
        )
        raw_pusd = pusd.functions.balanceOf(checksum).call()
        pusd_balance = raw_pusd / (10 ** STABLECOIN_DECIMALS)

        usdce = w3.eth.contract(
            address=w3.to_checksum_address(USDCE_POLYGON_ADDRESS),
            abi=_ERC20_ABI,
        )
        raw_usdce = usdce.functions.balanceOf(checksum).call()
        usdce_balance = raw_usdce / (10 ** STABLECOIN_DECIMALS)

        raw_matic = w3.eth.get_balance(checksum)
        matic = float(w3.from_wei(raw_matic, "ether"))

        return WalletState(
            available=True,
            address=checksum,
            pusd_balance=float(pusd_balance),
            usdce_balance=float(usdce_balance),
            matic_balance=matic,
            last_updated=time.time(),
            error=None,
        )
    except Exception as e:
        logger.warning(f"Wallet balance fetch failed: {e}")
        return _unavailable(str(e))


async def get_wallet_state(*, force: bool = False) -> WalletState:
    """Return the cached wallet state, refreshing if stale or `force=True`."""
    global _cache
    async with _cache_lock:
        fresh_enough = (
            _cache is not None
            and (time.time() - _cache.last_updated) < WALLET_CACHE_SECONDS
        )
        if fresh_enough and not force:
            return _cache  # type: ignore[return-value]
        _cache = await asyncio.to_thread(_fetch_sync)
        return _cache


async def refresh_balance() -> WalletState:
    """Force-refresh the cached wallet state."""
    return await get_wallet_state(force=True)


def cached_wallet_state() -> WalletState:
    """Return the last cached state synchronously (or unavailable if none)."""
    return _cache or _unavailable("Not yet polled")


def reset_cache() -> None:
    """Test helper: clear the cache so the next call refetches."""
    global _cache
    _cache = None
