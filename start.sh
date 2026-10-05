#!/bin/bash
# DELEG8 - Quick start script for frontend and backend

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}DELEG8 - AI Meeting Delegate${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check if Python venv is available
if [ ! -d ".venv" ]; then
    echo -e "${YELLOW}⚠ Virtual environment not found. Creating...${NC}"
    python3 -m venv .venv
fi

# Activate Python venv
source .venv/bin/activate

# Check if Postgres is running
if ! docker ps | grep -q deleg8-db; then
    echo -e "${YELLOW}⚠ Postgres container not running. Starting...${NC}"
    docker start deleg8-db 2>/dev/null || echo -e "${YELLOW}Note: Start Postgres with: docker start deleg8-db${NC}"
    sleep 2
fi

# Function to cleanup on exit
cleanup() {
    echo -e "${YELLOW}Stopping servers...${NC}"
    trap - SIGINT SIGTERM
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true
    echo -e "${GREEN}Done!${NC}"
    exit 0
}

trap cleanup SIGINT SIGTERM

echo -e "${GREEN}✓ Starting Backend (API)${NC}"
echo -e "${BLUE}  → http://127.0.0.1:8000${NC}"
echo -e "${BLUE}  → Docs: http://127.0.0.1:8000/docs${NC}"
python -m src.main serve &
BACKEND_PID=$!

sleep 2

echo ""
echo -e "${GREEN}✓ Starting Frontend (Web Console)${NC}"
echo -e "${BLUE}  → http://localhost:3000${NC}"
(cd web && npm run dev) &
FRONTEND_PID=$!

sleep 3

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✓ Both servers are running!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BLUE}Frontend:${NC} http://localhost:3000"
echo -e "${BLUE}Backend API:${NC} http://127.0.0.1:8000"
echo -e "${BLUE}API Docs:${NC} http://127.0.0.1:8000/docs"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop both servers${NC}"
echo ""

# Wait for processes
wait
