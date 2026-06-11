#!/bin/bash

# Frontend Stop Script

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

PID_FILE="frontend.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    echo -e "${YELLOW}Stopping frontend service (PID: $PID)...${NC}"
    
    if ps -p $PID > /dev/null; then
        kill $PID
        echo -e "${GREEN}✓ Service stopped${NC}"
    else
        echo -e "${YELLOW}⚠️  Process not running${NC}"
    fi
    rm "$PID_FILE"
else
    echo -e "${YELLOW}⚠️  No PID file found (${PID_FILE}). Checking port 5173...${NC}"
    # Fallback to port check
    PORT=5173
    occupied_pid=$(lsof -Pi :${PORT} -sTCP:LISTEN -t)
    if [ -n "$occupied_pid" ]; then
        echo -e "${YELLOW}Found process on port ${PORT} (PID: $occupied_pid). Stopping...${NC}"
        kill $occupied_pid
        echo -e "${GREEN}✓ Service stopped${NC}"
    else
        echo -e "${RED}❌ No service found running${NC}"
    fi
fi
