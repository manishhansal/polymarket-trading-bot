#!/usr/bin/env bash
# ============================================================
# polybot dev launcher (bash — git-bash, WSL, macOS, Linux)
# Boots backend + frontend together. Idempotent — safe to re-run.
# Usage:  bash scripts/dev.sh
# Stop:   Ctrl+C (both children get killed)
# ============================================================

set -euo pipefail

# Resolve repo root regardless of where the script is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

GREEN=$'\e[32m'; YELLOW=$'\e[33m'; DIM=$'\e[2m'; RESET=$'\e[0m'
step() { printf "\n%s==> %s%s\n" "$GREEN" "$1" "$RESET"; }
info() { printf "    %s%s%s\n" "$DIM" "$1" "$RESET"; }

# ------------------------------------------------------------
# 1. .env
# ------------------------------------------------------------
if [[ ! -f .env ]]; then
  step "Creating .env from .env.example"
  cp .env.example .env
else
  info ".env already present"
fi

# ------------------------------------------------------------
# 2. Python venv + deps
# ------------------------------------------------------------
# Pick the right venv python path (git-bash on Windows uses Scripts/, *nix uses bin/)
if [[ -x ".venv/Scripts/python.exe" ]]; then
  VENV_PY=".venv/Scripts/python.exe"
elif [[ -x ".venv/bin/python" ]]; then
  VENV_PY=".venv/bin/python"
else
  step "Creating Python virtualenv (.venv)"
  python -m venv .venv 2>/dev/null || python3 -m venv .venv
  if [[ -x ".venv/Scripts/python.exe" ]]; then
    VENV_PY=".venv/Scripts/python.exe"
  else
    VENV_PY=".venv/bin/python"
  fi
fi

step "Installing backend dependencies (fast after the first run)"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt

# ------------------------------------------------------------
# 3. Frontend deps
# ------------------------------------------------------------
if [[ ! -d frontend/node_modules ]]; then
  step "Installing frontend dependencies (npm install)"
  (cd frontend && npm install --silent)
else
  info "frontend/node_modules already present"
fi

# ------------------------------------------------------------
# 4. Launch both, kill cleanly on Ctrl+C
# ------------------------------------------------------------
step "Launching backend  → http://localhost:8000"
step "Launching frontend → http://localhost:5173"
echo
printf "%s  Press Ctrl+C to stop both.%s\n\n" "$YELLOW" "$RESET"

pids=()
cleanup() {
  echo
  printf "%s==> Shutting down…%s\n" "$YELLOW" "$RESET"
  for pid in "${pids[@]:-}"; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  # Give children a moment, then force-kill any survivors.
  sleep 1
  for pid in "${pids[@]:-}"; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

"$VENV_PY" -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000 &
pids+=($!)

(cd frontend && npm run dev) &
pids+=($!)

# Wait on whichever child exits first, then cleanup runs via trap.
wait -n
