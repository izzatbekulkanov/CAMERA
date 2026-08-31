#!/bin/bash
cd /home/smartgate/web/SmartGate
export OMP_NUM_THREADS=4
export MKL_NUM_THREADS=4
export OPENBLAS_NUM_THREADS=4

# Clean up stale processes on 8000 and 8002
fuser -k 8000/tcp 2>/dev/null || true
fuser -k 8002/tcp 2>/dev/null || true
sleep 1

# Start fast proxy in background
/home/smartgate/web/SmartGate/venv/bin/python -u proxy.py >> /home/smartgate/web/SmartGate/proxy.log 2>&1 &
PROXY_PID=$!

# Start daphne in background
/home/smartgate/web/SmartGate/venv/bin/daphne -b 0.0.0.0 -p 8002 core.asgi:application >> /home/smartgate/web/SmartGate/daphne.log 2>&1 &
DAPHNE_PID=$!

# Trap exit signals
trap "kill -9 $PROXY_PID $DAPHNE_PID 2>/dev/null || true" SIGINT SIGTERM EXIT

wait $DAPHNE_PID


