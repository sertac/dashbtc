#!/bin/bash
# BTC Signal Bot — Local Start Script
cd /Users/sertac/dashbtc

# Eski process'leri temizle
pkill -9 -f "gunicorn.*5007" 2>/dev/null
pkill -9 -f "btc35.*5007" 2>/dev/null
sleep 1

# Flask + Background loop (aynı process, farklı thread'ler)
.venv/bin/python -c "
import threading, sys
sys.path.insert(0, '/Users/sertac/dashbtc')
from btc35 import app, background_loop, db_init, load_signals

db_init()
load_signals()
print('DB + signals loaded')

t = threading.Thread(target=background_loop, daemon=True)
t.start()
print(f'Background loop: {t.is_alive()}')
print('🌐 http://localhost:5007')
app.run(host='0.0.0.0', port=5007, threaded=True, use_reloader=False)
"
