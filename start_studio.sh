#!/usr/bin/env bash
# ==============================================================================
# AI Film Studio - Hybrid Edition
# Local Production Startup Script for Ubuntu Linux
# ==============================================================================

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "=========================================================="
echo "🎬 STARTING AI FILM STUDIO - LOCAL PRODUCTION EDITION"
echo "=========================================================="

# 1. Environment & Directories
mkdir -p backend/data/watched_import backend/data/projects backend/data/exports backend/data/backups backend/data/temp

HOST="${STUDIO_HOST:-127.0.0.1}"
BACKEND_PORT="${PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

# 2. Check FFmpeg and FFprobe
if ! command -v ffmpeg &> /dev/null || ! command -v ffprobe &> /dev/null; then
    echo "❌ Error: FFmpeg and/or FFprobe not found in PATH."
    echo "👉 Please install FFmpeg on Ubuntu: sudo apt update && sudo apt install -y ffmpeg"
    exit 1
else
    echo "✅ FFmpeg & FFprobe detected: $(ffmpeg -version | head -n 1)"
fi

# 3. Check for Port Conflicts
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

# 4. Check AI Provider & Ollama Status
OLLAMA_URL="${OLLAMA_HOST:-http://127.0.0.1:11434}"
OLLAMA_MODEL_NAME="${OLLAMA_MODEL:-llama3.2}"

echo "🧠 Checking AI Provider connection ($OLLAMA_URL)..."
if curl -s --max-time 2 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    echo "✅ Local Ollama is online!"
    if curl -s "$OLLAMA_URL/api/tags" | grep -q "\"$OLLAMA_MODEL_NAME"; then
        echo "✅ Model '$OLLAMA_MODEL_NAME' is installed and ready."
    else
        echo "⚠️ Model '$OLLAMA_MODEL_NAME' not found in Ollama."
        echo "👉 Recommended: Run 'ollama pull $OLLAMA_MODEL_NAME' in another terminal."
        echo "👉 Studio will fall back to Offline Studio Engine until model is pulled."
    fi
else
    echo "ℹ️ Local Ollama is not running at $OLLAMA_URL."
    echo "👉 To enable local AI: Install from https://ollama.com and run 'ollama serve'."
    echo "👉 Studio will operate with built-in Offline Studio Engine (or Gemini if API key set)."
fi

# 5. Run Database Migrations
echo "🔄 Applying Alembic database migrations..."
export PYTHONPATH=.
backend/venv/bin/alembic upgrade head

# 6. Trap signals for clean shutdown
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

# 7. Start FastAPI Backend (Bound strictly to 127.0.0.1 by default)
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

# 8. Start Vite Frontend (Bound strictly to 127.0.0.1 by default)
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
