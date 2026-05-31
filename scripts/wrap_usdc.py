#!/usr/bin/env python
"""Wrap USDC.e -> pUSD on Polygon via Polymarket's CollateralOnramp.

Polymarket migrated its collateral token to **pUSD** on April 28, 2026.
All CLOB V2 orders settle in pUSD, so funds you deposit as USDC.e (or
plain USDC bridged onto Polygon) must be wrapped before they can trade.

What this script does:
  1. Reads PRIVATE_KEY + POLYGON_RPC_URL from .env
  2. Reads your USDC.e balance on Polygon
  3. Approves the CollateralOnramp (0x9307...8ee) to spend your USDC.e
  4. Calls wrap() to mint an equal amount of pUSD into the same wallet
  5. Prints the new pUSD balance

You can also do this through polymarket.com (the UI handles wrapping
automatically with a one-time approval). This script is the API-only path.

Usage:
    python scripts/wrap_usdc.py                # wrap entire USDC.e balance
    python scripts/wrap_usdc.py --amount 10    # wrap exactly 10 USDC.e
    python scripts/wrap_usdc.py --dry-run      # show plan without sending tx
"""

from __future__ import annotations

import argparse
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.wallet import (  # noqa: E402  — sys.path mutation above
    COLLATERAL_ONRAMP_ADDRESS,
    PUSD_POLYGON_ADDRESS,
    STABLECOIN_DECIMALS,
    USDCE_POLYGON_ADDRESS,
)


# ============================================================
# COLOR OUTPUT
# ============================================================


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            return True
        except Exception:
            return sys.stdout.isatty()
    return sys.stdout.isatty()


_C = _supports_color()
GREEN  = "\033[32m" if _C else ""
RED    = "\033[31m" if _C else ""
YELLOW = "\033[33m" if _C else ""
BLUE   = "\033[34m" if _C else ""
DIM    = "\033[2m"  if _C else ""
BOLD   = "\033[1m"  if _C else ""
RESET  = "\033[0m"  if _C else ""


def step(msg: str) -> None:
    print(f"\n{GREEN}==>{RESET} {BOLD}{msg}{RESET}")


def info(msg: str) -> None:
    print(f"    {DIM}{msg}{RESET}")


def warn(msg: str) -> None:
    print(f"    {YELLOW}!{RESET} {msg}")


def error(msg: str) -> None:
    print(f"    {RED}x{RESET} {msg}")


# ============================================================
# ABIs (minimal — only what we call)
# ============================================================

_ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "constant": True,
     "inputs": [{"name": "_owner", "type": "address"}],
     "outputs": [{"name": "balance", "type": "uint256"}]},
    {"name": "approve", "type": "function",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "allowance", "type": "function", "constant": True,
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
]

_ONRAMP_ABI = [
    {"name": "wrap", "type": "function",
     "inputs": [{"name": "_asset", "type": "address"},
                {"name": "_to", "type": "address"},
                {"name": "_amount", "type": "uint256"}],
     "outputs": []},
]


# ============================================================
# MAIN
# ============================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--amount", type=Decimal, default=None,
        help="USDC.e amount to wrap (default: entire balance)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without sending any transactions",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}{BLUE}polybot{RESET} {BOLD}— wrap USDC.e -> pUSD{RESET}")
    print(f"{DIM}{'-' * 60}{RESET}")

    # ---- env ----
    try:
        from backend.config import get_settings
        settings = get_settings()
    except Exception as e:
        error(f"Could not load settings: {e}")
        info("Make sure you're in the project root and .env exists.")
        return 1

    if not settings.private_key:
        error("PRIVATE_KEY is not set in .env.")
        info("Run `python scripts/generate_polymarket_keys.py --new-wallet --write` first.")
        return 1

    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError:
        error("web3 / eth_account not installed. Run: pip install -r requirements.txt")
        return 1

    step("Connecting to Polygon")
    w3 = Web3(Web3.HTTPProvider(settings.polygon_rpc_url, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        error(f"RPC {settings.polygon_rpc_url} unreachable.")
        return 1
    info(f"RPC: {settings.polygon_rpc_url}")
    info(f"Chain ID: {w3.eth.chain_id} (137 = Polygon mainnet)")
    if w3.eth.chain_id != 137:
        warn(f"Chain id is {w3.eth.chain_id}, not 137 — this is NOT Polygon mainnet.")

    account = Account.from_key(settings.private_key)
    address = w3.to_checksum_address(account.address)
    info(f"Wallet:  {GREEN}{address}{RESET}")

    onramp_addr = w3.to_checksum_address(COLLATERAL_ONRAMP_ADDRESS)
    usdce_addr = w3.to_checksum_address(USDCE_POLYGON_ADDRESS)
    pusd_addr = w3.to_checksum_address(PUSD_POLYGON_ADDRESS)

    usdce = w3.eth.contract(address=usdce_addr, abi=_ERC20_ABI)
    pusd = w3.eth.contract(address=pusd_addr, abi=_ERC20_ABI)
    onramp = w3.eth.contract(address=onramp_addr, abi=_ONRAMP_ABI)

    # ---- balances ----
    step("Reading current balances")
    matic = w3.from_wei(w3.eth.get_balance(address), "ether")
    usdce_raw = usdce.functions.balanceOf(address).call()
    pusd_raw = pusd.functions.balanceOf(address).call()
    usdce_h = usdce_raw / (10 ** STABLECOIN_DECIMALS)
    pusd_h = pusd_raw / (10 ** STABLECOIN_DECIMALS)
    info(f"USDC.e: {usdce_h:.4f}")
    info(f"pUSD:   {pusd_h:.4f}")
    info(f"MATIC:  {matic:.6f}  (need ~0.01 for gas)")

    if usdce_raw == 0:
        error("USDC.e balance is zero — nothing to wrap.")
        info("Send USDC.e to this wallet first, then re-run.")
        return 1
    if matic < Decimal("0.005"):
        error(f"Not enough MATIC ({matic:.6f}). Need at least ~0.01 for gas.")
        info("Send a small amount of MATIC to this wallet first.")
        return 1

    # ---- amount ----
    if args.amount is None:
        amount_raw = usdce_raw
        amount_human = usdce_h
    else:
        amount_raw = int(args.amount * (10 ** STABLECOIN_DECIMALS))
        amount_human = float(args.amount)
        if amount_raw > usdce_raw:
            error(f"Requested {amount_human} but only {usdce_h:.4f} USDC.e available.")
            return 1

    step("Plan")
    info(f"Wrap {GREEN}{amount_human:.4f} USDC.e{RESET} -> {GREEN}{amount_human:.4f} pUSD{RESET}")
    info(f"Via CollateralOnramp at {onramp_addr}")

    if args.dry_run:
        warn("--dry-run set, exiting without sending transactions.")
        return 0

    confirm = input(f"\n    Continue? [{GREEN}y{RESET}/N] ").strip().lower()
    if confirm not in ("y", "yes"):
        info("Aborted.")
        return 0

    # ---- approve ----
    step("Step 1/2: approve CollateralOnramp to spend USDC.e")
    current_allowance = usdce.functions.allowance(address, onramp_addr).call()
    info(f"Current allowance: {current_allowance / (10 ** STABLECOIN_DECIMALS):.4f}")
    if current_allowance >= amount_raw:
        info("Allowance already sufficient — skipping approve tx.")
    else:
        try:
            tx = usdce.functions.approve(onramp_addr, amount_raw).build_transaction({
                "from": address,
                "nonce": w3.eth.get_transaction_count(address),
                "chainId": w3.eth.chain_id,
                "gas": 80_000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            })
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            info(f"approve tx: {tx_hash.hex()}")
            info("Waiting for confirmation...")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status != 1:
                error(f"approve reverted (status {receipt.status})")
                return 1
            info(f"{GREEN}approved in block {receipt.blockNumber}{RESET}")
        except Exception as e:
            error(f"approve failed: {e}")
            return 1

    # ---- wrap ----
    step("Step 2/2: call wrap()")
    try:
        tx = onramp.functions.wrap(usdce_addr, address, amount_raw).build_transaction({
            "from": address,
            "nonce": w3.eth.get_transaction_count(address),
            "chainId": w3.eth.chain_id,
            "gas": 200_000,
            "maxFeePerGas": w3.eth.gas_price * 2,
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        info(f"wrap tx: {tx_hash.hex()}")
        info("Waiting for confirmation...")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        if receipt.status != 1:
            error(f"wrap reverted (status {receipt.status})")
            return 1
        info(f"{GREEN}wrapped in block {receipt.blockNumber}{RESET}")
    except Exception as e:
        error(f"wrap failed: {e}")
        return 1

    # ---- post-state ----
    step("New balances")
    usdce_after = usdce.functions.balanceOf(address).call() / (10 ** STABLECOIN_DECIMALS)
    pusd_after = pusd.functions.balanceOf(address).call() / (10 ** STABLECOIN_DECIMALS)
    info(f"USDC.e: {usdce_after:.4f}  (was {usdce_h:.4f})")
    info(f"pUSD:   {GREEN}{pusd_after:.4f}{RESET}  (was {pusd_h:.4f})")

    step("Done")
    info("The bot will pick up the new pUSD balance within 30 seconds.")
    info("If auto-mode is on and pUSD >= $5, the next trade will be LIVE.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
