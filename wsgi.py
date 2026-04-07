#!/usr/bin/env python3
"""
BTC/USDT Signal Bot — WSGI Entry Point
Production deployment (Render / Railway / Gunicorn)
Background loop başlatma gunicorn.conf.py'de post_worker_init ile yapılır.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Sadece Flask app'i import et — gunicorn.conf.py background_loop'u başlatır
from btc35 import app

# WSGI application
application = app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5007))
    app.run(host="0.0.0.0", port=port, threaded=True)
