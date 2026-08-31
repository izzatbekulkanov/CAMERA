#!/bin/bash
cd /home/smartgate/web/SmartGate
export OMP_NUM_THREADS=4
export MKL_NUM_THREADS=4
export OPENBLAS_NUM_THREADS=4

while true; do
    echo "[$(date)] Starting SmartGate server (Daphne:8002 & Proxy:8000)..." >> /home/smartgate/web/SmartGate/server.log
    
    # 1. Clean ports
    fuser -k 8000/tcp 2>/dev/null || true
    fuser -k 8002/tcp 2>/dev/null || true
    sleep 1
    
    # 2. Start fast proxy
    /home/smartgate/web/SmartGate/venv/bin/python -u /home/smartgate/web/SmartGate/proxy.py >> /home/smartgate/web/SmartGate/proxy.log 2>&1 &
    PROXY_PID=$!

    
    # 3. Start daphne
    /home/smartgate/web/SmartGate/venv/bin/daphne -b 0.0.0.0 -p 8002 core.asgi:application >> /home/smartgate/web/SmartGate/daphne.log 2>&1 &
    DAPHNE_PID=$!
    
    # Wait for daphne
    wait $DAPHNE_PID
    
    # If daphne exits, kill proxy and restart in 2 seconds
    kill -9 $PROXY_PID 2>/dev/null || true
    echo "[$(date)] Daphne exited. Restarting in 2s..." >> /home/smartgate/web/SmartGate/server.log
    sleep 2
done
