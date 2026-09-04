#!/usr/bin/env bash

echo "======================================================================="
echo "         ALGO BROKERAGE AUTOMATION & MONITORING SYSTEM"
echo "======================================================================="
echo ""

echo "[1/3] Checking Python Environment..."
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] python3 could not be found. Please install Python 3.10+."
    exit 1
fi

echo "[2/3] Installing/Verifying Dependencies..."
python3 -m pip install -r requirements.txt --quiet

echo "[3/3] Launching Web GUI Command Center..."
echo "Server will be accessible at: http://127.0.0.1:8000"
echo "Press Ctrl+C to stop."
echo ""

if command -v xdg-open &> /dev/null; then
    xdg-open "http://127.0.0.1:8000" &
elif command -v open &> /dev/null; then
    open "http://127.0.0.1:8000" &
fi

python3 -m uvicorn gui.server:app --host 127.0.0.1 --port 8000 --reload
