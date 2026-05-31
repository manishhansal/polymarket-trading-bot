# ============================================================
# polybot dev launcher (Windows PowerShell)
# Boots backend + frontend together. Idempotent — safe to re-run.
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
# Stop:   Ctrl+C in the launcher window (both children get killed)
# ============================================================

$ErrorActionPreference = "Stop"

# Resolve repo root regardless of where the script is invoked from.
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

function Write-Step([string]$msg) {
  Write-Host ""
  Write-Host "==> $msg" -ForegroundColor Green
}

function Write-Info([string]$msg) {
  Write-Host "    $msg" -ForegroundColor DarkGray
}

# ------------------------------------------------------------
# 1. .env
# ------------------------------------------------------------
if (-not (Test-Path ".env")) {
  Write-Step "Creating .env from .env.example"
  Copy-Item ".env.example" ".env"
} else {
  Write-Info ".env already present"
}

# ------------------------------------------------------------
# 2. Python venv + deps
# ------------------------------------------------------------
$venvPython = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
  Write-Step "Creating Python virtualenv (.venv)"
  python -m venv .venv
}

# pip install is idempotent — re-running is cheap when nothing has changed.
Write-Step "Installing backend dependencies (this is fast after the first run)"
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt

# ------------------------------------------------------------
# 3. Frontend deps
# ------------------------------------------------------------
$frontendDir = Join-Path $root "frontend"
if (-not (Test-Path (Join-Path $frontendDir "node_modules"))) {
  Write-Step "Installing frontend dependencies (npm install)"
  Push-Location $frontendDir
  try { npm install --silent } finally { Pop-Location }
} else {
  Write-Info "frontend/node_modules already present"
}

# ------------------------------------------------------------
# 4. Launch both processes
# ------------------------------------------------------------
Write-Step "Launching backend  → http://localhost:8000"
Write-Step "Launching frontend → http://localhost:5173"
Write-Host ""
Write-Host "  Press Ctrl+C to stop both." -ForegroundColor Yellow
Write-Host ""

$backend = Start-Process -PassThru -NoNewWindow `
  -FilePath $venvPython `
  -ArgumentList "-m", "uvicorn", "backend.main:app", "--reload", "--host", "0.0.0.0", "--port", "8000" `
  -WorkingDirectory $root

# Use cmd /c so we can reliably kill the npm child tree on Ctrl+C.
$frontend = Start-Process -PassThru -NoNewWindow `
  -FilePath "cmd.exe" `
  -ArgumentList "/c", "npm", "run", "dev" `
  -WorkingDirectory $frontendDir

# Best-effort cleanup on script exit.
$cleanup = {
  Write-Host ""
  Write-Host "==> Shutting down…" -ForegroundColor Yellow
  foreach ($p in @($backend, $frontend)) {
    if ($p -and -not $p.HasExited) {
      try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
  }
  # Kill stragglers (uvicorn reload workers, npm → vite child)
  Get-Process -Name "node", "python" -ErrorAction SilentlyContinue |
    Where-Object {
      $_.Path -like "$root*" -or $_.Path -like "*\.venv\Scripts\python.exe"
    } | Stop-Process -Force -ErrorAction SilentlyContinue
}

try {
  # Wait until either child dies.
  while (-not $backend.HasExited -and -not $frontend.HasExited) {
    Start-Sleep -Milliseconds 500
  }
} finally {
  & $cleanup
}
