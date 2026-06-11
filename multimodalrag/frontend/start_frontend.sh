#!/bin/bash

# Frontend Startup Script
# Starts the Vite development server in the background using nohup

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Config
SERVICE_NAME="frontend-dev"
PORT=5173
PID_FILE="frontend.pid"
LOG_FILE="frontend.log"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}🚀 Starting Frontend Service (Vite)${NC}"
echo -e "${BLUE}========================================${NC}\n"

# Check if port is in use
if lsof -Pi :${PORT} -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo -e "${YELLOW}⚠️  Port ${PORT} is already in use. Cleaning up...${NC}"
    
    occupied_pid=$(lsof -Pi :${PORT} -sTCP:LISTEN -t)
    if [ -n "$occupied_pid" ]; then
        echo -e "${YELLOW}  Stopping process (PID: $occupied_pid)...${NC}"
        kill $occupied_pid 2>/dev/null || true
        
        # Wait for it to die
        count=0
        while ps -p $occupied_pid > /dev/null 2>&1 && [ $count -lt 5 ]; do
            sleep 1
            count=$((count + 1))
        done
        
        # Force kill if needed
        if ps -p $occupied_pid > /dev/null 2>&1; then
            echo -e "${YELLOW}  Force killing...${NC}"
            kill -9 $occupied_pid 2>/dev/null || true
            sleep 1
        fi
        echo -e "${GREEN}  ✓ Port ${PORT} cleared${NC}"
    fi
fi

# Start Service
echo -e "${YELLOW}Starting npm run dev...${NC}"
nohup npm run dev > "${LOG_FILE}" 2>&1 &
PID=$!

# Save PID
echo $PID > "${PID_FILE}"

sleep 3

# Verify
if ps -p $PID > /dev/null; then
    echo -e "${GREEN}✓ Frontend started successfully (PID: $PID)${NC}"
    echo -e "${BLUE}  Log: ${SCRIPT_DIR}/${LOG_FILE}${NC}"
    echo -e "${BLUE}  URL: http://localhost:${PORT}${NC}"
else
    echo -e "${RED}❌ Frontend failed to start. Check log: ${SCRIPT_DIR}/${LOG_FILE}${NC}"
    rm -f "${PID_FILE}"
    exit 1
fi

echo -e "\n${BLUE}========================================${NC}\n"
