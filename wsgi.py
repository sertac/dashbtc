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

def _log(msg):
    """Flushed print for gunicorn compatibility."""
    print(msg, flush=True)

def _start_background_loop():
    """Background loop'u ayrı thread'de başlat."""
    def _run():
        try:
            _log("[WSGI] Background loop started")
            background_loop()
        except Exception as e:
            _log(f"[WSGI BG LOOP CRASH] {e}")
            import traceback
            _log(traceback.format_exc())
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    _log("[WSGI] Background loop thread started")

# DB init + signals load
try:
    db_init()
    load_signals()
    _log("[WSGI] Database initialized and signals loaded")
except Exception as e:
    _log(f"[WSGI ERROR] Initialization: {e}")

# Background loop başlat — Render'da tek worker olduğu için güvenli
_start_background_loop()

# WSGI application
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5007))
    app.run(host="0.0.0.0", port=port, threaded=True)
