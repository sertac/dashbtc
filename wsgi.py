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
    print(msg, flush=True)

def _start_bg():
    """Background loop'u başlat."""
    def _run():
        try:
            _log("[BG] Starting background_loop...")
            background_loop()
        except Exception as e:
            _log(f"[BG] CRASH: {e}")
            import traceback
            _log(traceback.format_exc())
    t = threading.Thread(target=_run, daemon=True, name="bg-loop")
    t.start()
    _log(f"[BG] Thread started, alive={t.is_alive()}")

# DB init
try:
    db_init()
    load_signals()
    _log("[WSGI] DB init and signals loaded")
except Exception as e:
    _log(f"[WSGI] DB init ERR: {e}")

# Background loop başlat — gunicorn worker'da da çalışsın
_start_bg()

# WSGI application
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5007))
    app.run(host="0.0.0.0", port=port, threaded=True)
