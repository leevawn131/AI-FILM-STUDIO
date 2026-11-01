#!/usr/bin/env bash
# AI Film Studio - Hybrid Edition
# Startup Script for Backend & Frontend

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

echo "=========================================================="
echo "🎬 STARTING AI FILM STUDIO - HYBRID EDITION"
echo "=========================================================="

# Ensure directories exist
mkdir -p backend/data/watched_import backend/data/projects backend/data/exports

# Check FFmpeg
if ! command -v ffmpeg &> /dev/null; then
    echo "⚠️ Warning: ffmpeg not found in PATH. Please install ffmpeg for video rendering."
else
    echo "✅ FFmpeg detected: $(ffmpeg -version | head -n 1)"
fi

# Activate Python venv & run backend
echo "🚀 Launching FastAPI Backend on http://127.0.0.1:8000..."
export PYTHONPATH=.
backend/venv/bin/uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Trap signals to kill child processes
cleanup() {
    echo ""
    echo "🛑 Shutting down AI Film Studio servers..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# Start Vite Frontend
echo "✨ Launching Vite React Cinematic Frontend on http://localhost:5173..."
cd frontend
npm run dev -- --host 0.0.0.0 --port 5173 &
FRONTEND_PID=$!
cd ..

echo "=========================================================="
echo "🎬 STUDIO ONLINE!"
echo "👉 Frontend Interface: http://localhost:5173"
echo "👉 Backend API & Docs: http://127.0.0.1:8000/docs"
echo "👉 Watched Import Folder: $DIR/backend/data/watched_import"
echo "=========================================================="
echo "Press Ctrl+C to stop both servers."

wait
