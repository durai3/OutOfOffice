#!/bin/bash
# DELEG8 - one-command start for frontend + backend.
# Usage: ./start.sh
#
# Does everything needed from a cold machine: kills anything already bound to
# our ports, makes sure the Postgres container exists and is running, installs
# Python + npm dependencies (fast no-ops if already satisfied), starts both
# servers, and waits for each to actually answer before declaring success.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

BACKEND_PORT=8000
FRONTEND_PORT=3000
BACKEND_LOG="$PROJECT_DIR/.backend.log"
FRONTEND_LOG="$PROJECT_DIR/.frontend.log"

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}DELEG8 - AI Meeting Delegate${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# --- 1. Kill anything already using our ports (stale runs, crashed servers) ---
echo -e "${YELLOW}→ Clearing ports ${BACKEND_PORT} and ${FRONTEND_PORT}...${NC}"
for PORT in $BACKEND_PORT $FRONTEND_PORT; do
    PIDS=$(lsof -ti ":$PORT" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "$PIDS" | xargs kill -9 2>/dev/null || true
        echo -e "  killed existing process(es) on port $PORT"
    fi
done
pkill -f "src.main serve" 2>/dev/null || true
sleep 1

# --- 2. Docker: make sure the Postgres container exists and is running ---
echo -e "${YELLOW}→ Checking database container...${NC}"
if ! docker info >/dev/null 2>&1; then
    echo -e "${RED}✗ Docker is not running. Open Docker Desktop and re-run ./start.sh${NC}"
    exit 1
fi

if docker ps -a --format '{{.Names}}' | grep -qx deleg8-db; then
    if ! docker ps --format '{{.Names}}' | grep -qx deleg8-db; then
        docker start deleg8-db >/dev/null
        echo -e "  started existing deleg8-db container"
    else
        echo -e "  deleg8-db already running"
    fi
else
    docker run -d --name deleg8-db -e POSTGRES_USER=deleg8 \
        -e POSTGRES_PASSWORD=deleg8 -e POSTGRES_DB=deleg8 \
        -p 55432:5432 -v deleg8_pgdata:/var/lib/postgresql/data postgres:16-alpine >/dev/null
    echo -e "  created deleg8-db container"
fi

echo -n "  waiting for Postgres to accept connections"
for i in $(seq 1 30); do
    if docker exec deleg8-db pg_isready -U deleg8 >/dev/null 2>&1; then
        echo " - ready"
        break
    fi
    echo -n "."
    sleep 1
    if [ "$i" -eq 30 ]; then
        echo -e " ${RED}timed out${NC}"
        exit 1
    fi
done

# --- 3. Python dependencies ---
echo -e "${YELLOW}→ Checking Python environment...${NC}"
if [ ! -d ".venv" ]; then
    echo -e "  creating virtual environment"
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt

# --- 4. Frontend dependencies ---
echo -e "${YELLOW}→ Checking frontend dependencies...${NC}"
(cd web && npm install --no-audit --no-fund --loglevel=error)

# --- 5. Cleanup handler ---
BACKEND_PID=""
FRONTEND_PID=""
cleanup() {
    echo ""
    echo -e "${YELLOW}Stopping servers...${NC}"
    trap - SIGINT SIGTERM EXIT
    [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null
    for PORT in $BACKEND_PORT $FRONTEND_PORT; do
        PIDS=$(lsof -ti ":$PORT" 2>/dev/null || true)
        [ -n "$PIDS" ] && echo "$PIDS" | xargs kill -9 2>/dev/null || true
    done
    echo -e "${GREEN}Done.${NC}"
    exit 0
}
trap cleanup SIGINT SIGTERM

# --- 6. Start backend, wait for a real health response ---
echo ""
echo -e "${GREEN}✓ Starting backend...${NC}"
: > "$BACKEND_LOG"
python -m src.main serve > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

echo -n "  waiting for http://127.0.0.1:${BACKEND_PORT}"
BACKEND_UP=0
for i in $(seq 1 90); do
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo -e " ${RED}backend process exited early${NC}"
        echo -e "${RED}--- last 40 lines of ${BACKEND_LOG} ---${NC}"
        tail -40 "$BACKEND_LOG"
        exit 1
    fi
    if curl -sf "http://127.0.0.1:${BACKEND_PORT}/api/v1/health" >/dev/null 2>&1; then
        echo " - up"
        BACKEND_UP=1
        break
    fi
    echo -n "."
    sleep 1
done
if [ "$BACKEND_UP" -eq 0 ]; then
    echo -e " ${RED}timed out waiting for backend${NC}"
    echo -e "${RED}--- last 40 lines of ${BACKEND_LOG} ---${NC}"
    tail -40 "$BACKEND_LOG"
    cleanup
fi

# --- 7. Start frontend, wait for a real response ---
echo -e "${GREEN}✓ Starting frontend...${NC}"
: > "$FRONTEND_LOG"
(cd web && npm run dev > "$FRONTEND_LOG" 2>&1) &
FRONTEND_PID=$!

echo -n "  waiting for http://localhost:${FRONTEND_PORT}"
FRONTEND_UP=0
for i in $(seq 1 60); do
    if ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo -e " ${RED}frontend process exited early${NC}"
        echo -e "${RED}--- last 40 lines of ${FRONTEND_LOG} ---${NC}"
        tail -40 "$FRONTEND_LOG"
        cleanup
    fi
    if curl -sf "http://localhost:${FRONTEND_PORT}" >/dev/null 2>&1; then
        echo " - up"
        FRONTEND_UP=1
        break
    fi
    echo -n "."
    sleep 1
done
if [ "$FRONTEND_UP" -eq 0 ]; then
    echo -e " ${RED}timed out waiting for frontend${NC}"
    echo -e "${RED}--- last 40 lines of ${FRONTEND_LOG} ---${NC}"
    tail -40 "$FRONTEND_LOG"
    cleanup
fi

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✓ Everything is running!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${BLUE}Frontend:${NC}   http://localhost:${FRONTEND_PORT}"
echo -e "${BLUE}Backend API:${NC} http://127.0.0.1:${BACKEND_PORT}"
echo -e "${BLUE}API Docs:${NC}    http://127.0.0.1:${BACKEND_PORT}/docs"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop both servers${NC}"
echo ""

wait
