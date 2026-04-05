#!/usr/bin/env python3
"""
BTC/USDT Signal Bot — WSGI Entry Point
Production deployment (Render / Railway / Gunicorn)
Background loop gunicorn.conf.py → post_worker_init ile başlatılır
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from btc35 import app, db_init, load_signals

def _log(msg):
    print(msg, flush=True)

# DB init + signals load
try:
    db_init()
    load_signals()
    _log("[WSGI] Database initialized and signals loaded")
except Exception as e:
    _log(f"[WSGI ERROR] Initialization: {e}")

# WSGI application
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5007))
    from btc35 import background_loop
    import threading
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()
    _log("[WSGI] Background loop started (local mode)")
    app.run(host="0.0.0.0", port=port, threaded=True)
