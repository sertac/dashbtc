#!/usr/bin/env python3
"""
BTC/USDT Signal Bot — WSGI Entry Point
Production deployment (Render / Railway / Gunicorn)
"""
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from btc35 import app, db_init, load_signals, background_loop

def _start_background_loop():
    """Background loop'u ayrı thread'de başlat."""
    def _run():
        try:
            background_loop()
        except Exception as e:
            print(f"[WSGI BG LOOP CRASH] {e}")
            import traceback
            traceback.print_exc()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    print("[WSGI] Background loop thread started")

# DB init + signals load
try:
    db_init()
    load_signals()
    print("[WSGI] Database initialized and signals loaded")
except Exception as e:
    print(f"[WSGI ERROR] Initialization: {e}")

# Background loop başlat (sadece ilk worker'da — Render'da tek worker var)
_worker_id = os.environ.get("GUNICORN_WORKER_ID", "0")
if _worker_id == "0":
    _start_background_loop()

# WSGI application
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5007))
    _start_background_loop()
    app.run(host="0.0.0.0", port=port, threaded=True)
