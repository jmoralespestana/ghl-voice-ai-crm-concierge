#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

PORT="${APP_PORT:-8090}"
HOST="${APP_HOST:-127.0.0.1}"
MODE="${1:-foreground}"

if [[ "$MODE" == "--stop" ]]; then
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
  fi
  echo "🛑 Local server on port ${PORT} stopped."
  exit 0
fi

# 1. Resolve Python interpreter (.venv or uv sync)
if [[ -n "${PYTHON_BIN:-}" ]] && [[ -x "${PYTHON_BIN}" ]]; then
  PY_BIN="${PYTHON_BIN}"
elif [[ -x "$PROJECT_DIR/.venv/bin/python" ]] && "$PROJECT_DIR/.venv/bin/python" -c "import fastapi, uvicorn, websockets, httpx, google.genai" 2>/dev/null; then
  PY_BIN="$PROJECT_DIR/.venv/bin/python"
else
  echo "📦 Installing Python dependencies with uv..."
  uv sync
  PY_BIN="$PROJECT_DIR/.venv/bin/python"
fi

# 2. Verify .env and SQLite WAL data directory
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  echo "❌ Missing $PROJECT_DIR/.env. Copy .env.example to .env and configure GHL_ACCESS_TOKEN and GHL_LOCATION_ID (or set CRM_PROVIDER=local_only)."
  exit 1
fi
mkdir -p "$PROJECT_DIR/data"

# 3. Preflight check against GoHighLevel Cloud API v2 + optional Google Cloud ADC
echo "🔍 Running live connectivity preflight (GoHighLevel API v2 + optional Vertex AI)..."
PYTHONPATH="$PROJECT_DIR" "$PY_BIN" - <<'PY'
import sys
import httpx
from app.config import get_settings

cfg = get_settings()
if cfg.CRM_PROVIDER == "dual_ghl" and cfg.ghl_live_configured:
    resp = None
    last_err = None
    for _ in range(3):
        try:
            resp = httpx.get(
                f"{cfg.GHL_BASE_URL.rstrip('/')}/contacts/",
                params={"locationId": cfg.GHL_LOCATION_ID.strip(), "limit": 1},
                headers={
                    "Authorization": f"Bearer {cfg.GHL_ACCESS_TOKEN.strip()}",
                    "Version": cfg.GHL_API_VERSION,
                    "Accept": "application/json",
                },
                timeout=18.0,
            )
            break
        except httpx.HTTPError as exc:
            last_err = exc
            import time
            time.sleep(1.5)

    if resp is None:
        print(f"⚠️ Temporary timeout in GoHighLevel preflight ({last_err}); starting server (will retry per turn).")
    elif not resp.is_success:
        print(f"❌ GoHighLevel API v2 rejected credentials (HTTP {resp.status_code}): {resp.text[:200]}")
        sys.exit(1)
    else:
        print(
            f"✅ GoHighLevel API v2 LIVE CONNECTED (HTTP {resp.status_code}) | "
            f"Location={cfg.GHL_LOCATION_ID} | Pipeline={cfg.GHL_PIPELINE_ID or 'N/A'}"
        )
else:
    print("ℹ️ Running in SANDBOX_PREVIEW mode (configure GHL_ACCESS_TOKEN + GHL_LOCATION_ID in .env for live GHL v2 sync).")

if cfg.VERTEX_USE_ADC and cfg.VERTEX_PROJECT_ID.strip():
    try:
        import google.auth
        creds, _ = google.auth.default()
        assert creds is not None
        print(f"✅ Google Cloud ADC active | Vertex Project={cfg.VERTEX_PROJECT_ID} ({cfg.VERTEX_LOCATION}) | Model={cfg.GEMINI_MODEL}")
    except Exception as exc:
        print(f"⚠️ Google Cloud ADC warning ({exc}). Run: gcloud auth application-default login")
PY

# 4. Release port if occupied by a stale instance
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
fi

GHL_LOC_PREVIEW="$(grep -E '^GHL_LOCATION_ID=' "$PROJECT_DIR/.env" | head -n1 | cut -d= -f2- | tr -d '[:space:]' || true)"

echo "================================================================================"
echo "🚀 Horizon Estates — GoHighLevel API v2, Dual Voice AI & HITL Concierge"
echo "   Web UI (Buyer Portal + HITL Cockpit) : http://localhost:${PORT}/"
echo "   GoHighLevel Sub-Account Mirror       : https://app.gohighlevel.com/v2/location/${GHL_LOC_PREVIEW:-<YOUR_LOCATION_ID>}/opportunities/list"
echo "   OpenAPI / Swagger                    : http://localhost:${PORT}/docs"
echo "================================================================================"

if [[ "$MODE" == "--bg" ]]; then
  LOG_FILE="$PROJECT_DIR/data/uvicorn.log"
  nohup env PYTHONPATH="$PROJECT_DIR" "$PY_BIN" -m uvicorn app.main:app \
    --host "$HOST" --port "$PORT" --timeout-graceful-shutdown 1 >"$LOG_FILE" 2>&1 &
  BG_PID=$!
  for _ in {1..20}; do
    if curl -fsS "http://${HOST}:${PORT}/api/v1/lots" >/dev/null 2>&1; then
      echo "✅ Background server active (PID=${BG_PID}). Logs: tail -f $LOG_FILE"
      echo "🛑 Stop anytime with: ./scripts/start_local_demo.sh --stop"
      exit 0
    fi
    sleep 0.3
  done
  echo "❌ Server did not respond in time. Check $LOG_FILE"
  exit 1
fi

exec env PYTHONPATH="$PROJECT_DIR" "$PY_BIN" -m uvicorn app.main:app \
  --host "$HOST" --port "$PORT" --timeout-graceful-shutdown 1
