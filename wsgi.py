#!/usr/bin/env python3
"""
BTC/USDT Signal Bot - WSGI Entry Point
Production deployment file
"""

import sys
import os

# Add project directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import Flask app from btc35.py
from btc35 import app, load_signals, db_init

# Initialize database and load signals
try:
    db_init()
    load_signals()
    print("[WSGI] Database initialized and signals loaded")
except Exception as e:
    print(f"[WSGI ERROR] Initialization: {e}")

# Expose application for WSGI server
application = app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5007, threaded=True)
