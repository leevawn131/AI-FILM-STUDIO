#!/usr/bin/env bash
# ==============================================================================
# AI FILM STUDIO — LOCAL WEB APPLICATION, GOOGLE CLOUD AI ECOSYSTEM
# Production Startup Script for Ubuntu/Debian Linux
# ==============================================================================

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "=========================================================="
echo "🎬 STARTING AI FILM STUDIO"
echo "   Architecture: Local Web Application + Google Cloud AI"
echo "=========================================================="

# 1. Environment & Directories
mkdir -p backend/data/watched_import backend/data/projects backend/data/exports backend/data/backups backend/data/temp

HOST="${STUDIO_HOST:-127.0.0.1}"
BACKEND_PORT="${PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

# 2. Check Backend Dependencies
if [ ! -d "backend/venv" ] || [ ! -f "backend/venv/bin/uvicorn" ]; then
    echo "❌ Error: Python virtual environment not found in backend/venv."
    echo "👉 Please set up: cd backend && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi
echo "✅ Backend Python virtualenv detected."

# 3. Check Frontend Dependencies
if [ ! -d "frontend/node_modules" ]; then
    echo "❌ Error: Frontend node_modules not found."
    echo "👉 Please install frontend packages: cd frontend && npm install"
    exit 1
fi
echo "✅ Frontend Node dependencies detected."

# 4. Check FFmpeg and FFprobe (Local Video Processing)
if ! command -v ffmpeg &> /dev/null || ! command -v ffprobe &> /dev/null; then
    echo "❌ Error: FFmpeg and/or FFprobe not found in PATH."
    echo "👉 Please install FFmpeg on Ubuntu: sudo apt update && sudo apt install -y ffmpeg"
    exit 1
else
    echo "✅ Local FFmpeg & FFprobe detected: $(ffmpeg -version | head -n 1)"
fi

# 5. Check for Port Conflicts
check_port() {
    local port=$1
    if command -v lsof &> /dev/null; then
        if lsof -i:"$port" -sTCP:LISTEN -t >/dev/null ; then
            echo "⚠️ Port $port is already in use by process $(lsof -i:"$port" -sTCP:LISTEN -t | tr '\n' ' ')"
            return 1
        fi
    elif command -v ss &> /dev/null; then
        if ss -tulpn | grep -q ":$port "; then
            echo "⚠️ Port $port is already in use."
            return 1
        fi
    fi
    return 0
}

if ! check_port "$BACKEND_PORT"; then
    echo "❌ Cannot start backend: Port $BACKEND_PORT is occupied. Please free port $BACKEND_PORT or set PORT=..."
    exit 1
fi

if ! check_port "$FRONTEND_PORT"; then
    echo "❌ Cannot start frontend: Port $FRONTEND_PORT is occupied. Please free port $FRONTEND_PORT or set FRONTEND_PORT=..."
    exit 1
fi

# 6. Check Google Cloud AI (Gemini) Configuration
ENV_FILE="backend/.env"
GEMINI_KEY="${GEMINI_API_KEY}"
if [ -z "$GEMINI_KEY" ] && [ -f "$ENV_FILE" ]; then
    GEMINI_KEY=$(grep -E "^GEMINI_API_KEY=" "$ENV_FILE" | cut -d'=' -f2- | tr -d ' "' || true)
fi

echo "🧠 Checking Google Cloud AI Intelligence Configuration..."
if [ -n "$GEMINI_KEY" ]; then
    echo "✅ Google Gemini API Key configured."
    # Fast network check (timeout 3s)
    if curl -s --max-time 3 "https://generativelanguage.googleapis.com" >/dev/null 2>&1; then
        echo "✅ Connected to Google Generative Language service."
    else
        echo "⚠️ Note: Google Cloud AI endpoint connectivity test timed out. Cloud operations will retry automatically."
    fi
else
    echo "ℹ️ GEMINI_API_KEY is not configured yet."
    echo "👉 AI Film Studio will open in Local Workspace mode (full access to projects, media, and timeline)."
    echo "👉 You can enter your Gemini API key in the UI header anytime to activate cloud AI features."
fi

# Check optional Ollama status only if explicitly enabled
OLLAMA_IS_ENABLED="${OLLAMA_ENABLED:-false}"
if [ "$OLLAMA_IS_ENABLED" = "true" ]; then
    OLLAMA_URL="${OLLAMA_HOST:-http://127.0.0.1:11434}"
    if curl -s --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
        echo "✅ Optional local Ollama fallback is online ($OLLAMA_URL)."
    else
        echo "ℹ️ Optional Ollama is enabled but not responding at $OLLAMA_URL."
    fi
else
    echo "ℹ️ Local Ollama fallback is disabled (OLLAMA_ENABLED=false). Ollama is NOT required."
fi

# 7. Run Database Migrations
echo "🔄 Applying Alembic database migrations..."
export PYTHONPATH=.
backend/venv/bin/alembic upgrade head

# 8. Trap signals for clean shutdown
cleanup() {
    echo ""
    echo "🛑 Gracefully shutting down AI Film Studio servers..."
    if [ -n "$BACKEND_PID" ]; then
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
    if [ -n "$FRONTEND_PID" ]; then
        kill "$FRONTEND_PID" 2>/dev/null || true
    fi
    wait 2>/dev/null || true
    echo "✅ Studio stopped."
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# 9. Start FastAPI Backend (Strictly bound to 127.0.0.1)
echo "🚀 Launching FastAPI Backend on http://$HOST:$BACKEND_PORT..."
backend/venv/bin/uvicorn backend.app.main:app --host "$HOST" --port "$BACKEND_PORT" &
BACKEND_PID=$!

# Wait for backend health check
for i in {1..20}; do
    if curl -s "http://$HOST:$BACKEND_PORT/api/health" | grep -q '"status":"healthy"'; then
        echo "✅ Backend is healthy and responding!"
        break
    fi
    sleep 0.5
done

# 10. Start Vite Frontend (Strictly bound to 127.0.0.1)
echo "✨ Launching Vite React Cinematic Frontend on http://$HOST:$FRONTEND_PORT..."
cd frontend
npm run dev -- --host "$HOST" --port "$FRONTEND_PORT" &
FRONTEND_PID=$!
cd ..

echo "=========================================================="
echo "🎬 STUDIO ONLINE & READY!"
echo "👉 Frontend Interface: http://$HOST:$FRONTEND_PORT"
echo "👉 Backend API Docs:   http://$HOST:$BACKEND_PORT/docs"
echo "👉 Watched Ingest Dir: $DIR/backend/data/watched_import"
echo "👉 Project Backups:    $DIR/backend/data/backups"
echo "=========================================================="
echo "Press Ctrl+C to stop both servers."

wait
