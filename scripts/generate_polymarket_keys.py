#!/usr/bin/env python
"""Polymarket API key generator — interactive helper.

What this script does:
  1. Reads your Polygon wallet private key (from .env, or prompts for it)
  2. Derives the wallet address and shows it for verification
  3. Connects to the Polymarket CLOB V2 with L1 (wallet-signature) auth
  4. Calls create_or_derive_api_key() — generates new L2 API creds, OR
     deterministically derives the ones that already exist for your wallet
  5. Prints the three credentials, ready to paste into .env
  6. Optionally writes them into .env for you (with confirmation)

What this script does NOT do:
  * Send your private key anywhere. The signature happens locally; only the
    signed challenge is sent to Polymarket. The key never leaves your machine.
  * Write to disk without your explicit "y" confirmation.
  * Submit any trades.

Usage:
    python scripts/generate_polymarket_keys.py
    python scripts/generate_polymarket_keys.py --write       # auto-write to .env
    python scripts/generate_polymarket_keys.py --new-wallet  # create a fresh wallet first
    python scripts/generate_polymarket_keys.py --host https://clob.polymarket.com
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"


# ============================================================
# COLOR OUTPUT (works on Windows 10+ + macOS + Linux)
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
# .env HANDLING
# ============================================================


def parse_env_file(path: Path) -> dict[str, str]:
    """Return a dict of KEY=VALUE pairs from a .env file."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Overwrite KEY=VALUE pairs in `path`, preserving comments and order."""
    if path.exists():
        original = path.read_text(encoding="utf-8").splitlines(keepends=False)
    else:
        original = []
    written: set[str] = set()
    new_lines: list[str] = []
    for line in original:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            new_lines.append(f"{key}={updates[key]}")
            written.add(key)
        else:
            new_lines.append(line)
    for k, v in updates.items():
        if k not in written:
            new_lines.append(f"{k}={v}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ============================================================
# PRIVATE KEY HANDLING
# ============================================================


_PK_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def validate_private_key(pk: str) -> str:
    """Strip whitespace, ensure 0x prefix, validate hex length. Raises ValueError."""
    pk = pk.strip()
    if not _PK_RE.match(pk):
        raise ValueError(
            "Private key must be a 64-character hex string (with or without 0x prefix). "
            f"Got {len(pk)} chars."
        )
    if not pk.startswith("0x"):
        pk = "0x" + pk
    return pk


def derive_address(pk: str) -> str:
    """Derive the Ethereum address from a private key. Returns checksummed address."""
    from eth_account import Account
    return Account.from_key(pk).address


def create_new_wallet() -> tuple[str, str]:
    """Generate a brand-new wallet using OS cryptographic randomness.

    Returns (private_key_hex_with_0x, checksummed_address).
    """
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    acct = Account.create()
    return acct.key.hex() if acct.key.hex().startswith("0x") else "0x" + acct.key.hex(), acct.address


def prompt_for_private_key(env_pk: str = "") -> str:
    """Get a valid private key from either .env or interactive input."""
    if env_pk:
        try:
            validated = validate_private_key(env_pk)
            address = derive_address(validated)
            info(f"Found PRIVATE_KEY in .env → wallet {address}")
            ans = input(
                f"    Use this wallet? [{GREEN}Y{RESET}/n] "
            ).strip().lower()
            if ans in ("", "y", "yes"):
                return validated
        except Exception as e:
            warn(f"PRIVATE_KEY in .env is invalid ({e}). Falling back to prompt.")

    print(
        f"\n    {DIM}Enter your Polygon wallet private key. "
        f"Input is hidden.{RESET}"
    )
    print(
        f"    {DIM}This is the key for a wallet that will (a) hold USDC.e on "
        f"Polygon, and{RESET}"
    )
    print(
        f"    {DIM}(b) sign Polymarket orders. Use a dedicated wallet — "
        f"not your main one.{RESET}\n"
    )
    while True:
        pk = getpass.getpass("    PRIVATE_KEY: ")
        try:
            return validate_private_key(pk)
        except ValueError as e:
            error(str(e))


# ============================================================
# MAIN FLOW
# ============================================================


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--host", default="https://clob.polymarket.com",
        help="Polymarket CLOB host (default: %(default)s)",
    )
    parser.add_argument(
        "--chain-id", type=int, default=137,
        help="Polygon chain id (137 mainnet, 80002 Amoy testnet)",
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Write the generated keys to .env without asking",
    )
    parser.add_argument(
        "--new-wallet", action="store_true",
        help="Generate a fresh Polygon wallet first (use this if you don't have one)",
    )
    args = parser.parse_args()

    print(f"\n{BOLD}{BLUE}polybot{RESET} {BOLD}— Polymarket API key generator{RESET}")
    print(f"{DIM}{'-' * 60}{RESET}")
    print(f"    host:       {args.host}")
    print(f"    chain id:   {args.chain_id}")
    print(f"    .env path:  {ENV_FILE}")

    # ---- 1. ensure .env exists ----
    step("Checking for .env")
    if not ENV_FILE.exists():
        if ENV_EXAMPLE.exists():
            info("No .env — copying from .env.example")
            ENV_FILE.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"),
                                encoding="utf-8")
        else:
            warn("Neither .env nor .env.example found — keys will be printed only.")

    env = parse_env_file(ENV_FILE)

    # ---- 2. private key ----
    if args.new_wallet:
        step("Generating a brand-new Polygon wallet")
        info("Using OS cryptographic randomness (eth_account.Account.create()).")
        info("This wallet has never touched a browser or another machine.")
        pk, addr = create_new_wallet()
        print()
        print(f"    {BOLD}NEW WALLET CREATED{RESET}")
        print(f"    address:     {GREEN}{addr}{RESET}")
        print(f"    private key: {YELLOW}{pk}{RESET}")
        print()
        print(f"    {RED}{BOLD}!! BACK THIS UP NOW !!{RESET}")
        print(f"    {DIM}Anyone with this private key controls the wallet.")
        print(f"    Save it in a password manager. If you lose it, the funds")
        print(f"    in this wallet are gone forever.{RESET}")
        print()
        ack = input(
            f"    Type {GREEN}I SAVED IT{RESET} to continue: "
        ).strip()
        if ack != "I SAVED IT":
            error("Aborted — wallet was not saved. Re-run when ready.")
            return 1
    else:
        step("Loading Polygon wallet private key")
        try:
            pk = prompt_for_private_key(env.get("PRIVATE_KEY", ""))
        except KeyboardInterrupt:
            print("\n    aborted.")
            return 130

    try:
        address = derive_address(pk)
    except Exception as e:
        error(f"Could not derive address from private key: {e}")
        return 1
    info(f"Wallet address: {GREEN}{address}{RESET}")

    # ---- 3. CLOB V2 ----
    step("Connecting to Polymarket CLOB V2")
    try:
        from py_clob_client_v2 import ClobClient
    except ImportError:
        error(
            "py-clob-client-v2 is not installed. Run:\n"
            "        pip install py-clob-client-v2"
        )
        return 1

    try:
        client = ClobClient(host=args.host, chain_id=args.chain_id, key=pk)
    except Exception as e:
        error(f"Failed to construct ClobClient: {e}")
        return 1

    step("Requesting API credentials")
    info("This signs an EIP-712 challenge with your wallet locally and sends")
    info("the signature to Polymarket. Your private key never leaves the machine.")
    try:
        creds = client.create_or_derive_api_key()
    except Exception as e:
        error(f"Polymarket API rejected the request: {e}")
        info("Common causes:")
        info("  - Wallet has never interacted with Polymarket → visit polymarket.com")
        info("    once with this wallet and approve the terms of service.")
        info("  - Network issue / CLOB host unreachable.")
        info("  - Wrong chain id (must be 137 for mainnet, 80002 for Amoy).")
        return 1

    api_key = getattr(creds, "api_key", None) or getattr(creds, "key", None)
    api_secret = getattr(creds, "api_secret", None) or getattr(creds, "secret", None)
    api_passphrase = (
        getattr(creds, "api_passphrase", None)
        or getattr(creds, "passphrase", None)
    )
    if not (api_key and api_secret and api_passphrase):
        error("Unexpected credentials shape — got: " + repr(creds))
        return 1

    # ---- 4. Display + write ----
    step("Got credentials!")
    print(f"\n    {BOLD}Paste these into your .env file:{RESET}\n")
    print(f"    POLYMARKET_API_KEY={GREEN}{api_key}{RESET}")
    print(f"    POLYMARKET_SECRET={GREEN}{api_secret}{RESET}")
    print(f"    POLYMARKET_PASSPHRASE={GREEN}{api_passphrase}{RESET}")
    print(f"    PRIVATE_KEY={DIM}{pk}{RESET}")
    print()

    should_write = args.write
    if not should_write and ENV_FILE.exists():
        ans = input(
            f"    Write these to {ENV_FILE.name}? [{GREEN}Y{RESET}/n] "
        ).strip().lower()
        should_write = ans in ("", "y", "yes")

    if should_write:
        try:
            update_env_file(ENV_FILE, {
                "PRIVATE_KEY":            pk,
                "POLYMARKET_API_KEY":     api_key,
                "POLYMARKET_SECRET":      api_secret,
                "POLYMARKET_PASSPHRASE":  api_passphrase,
            })
            info(f"{GREEN}wrote {ENV_FILE}{RESET}")
        except Exception as e:
            error(f"Could not write .env: {e}")
            warn("Copy-paste the values above instead.")
            return 1

    step("Next steps")
    info(f"1. Fund the wallet ({address}) with USDC.e on Polygon")
    info(f"   USDC.e contract: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
    info("2. Send 0.5+ MATIC to the same address for gas")
    info("3. Visit polymarket.com once with this wallet and accept TOS")
    info("4. Restart the bot — auto-mode will go LIVE once balance ≥ $5")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
