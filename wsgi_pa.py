#!/usr/bin/env python3
"""
BTC/USDT Signal Bot - PythonAnywhere WSGI Entry Point
"""

import sys
import os

# Project directory
project_home = '/home/sertac/dashbtc'
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Activate virtualenv
activate_env = os.path.join(project_home, 'venv/bin/activate_this.py')
if os.path.exists(activate_env):
    with open(activate_env) as f:
        exec(f.read(), {'__file__': activate_env})

# Environment variables
os.environ['FLASK_ENV'] = 'production'
os.environ['SECRET_KEY'] = 'pythonanywhere_secret_2026_sertac'
os.environ['PORT'] = '5007'

# Change to project directory
os.chdir(project_home)

# Import Flask app (background loop starts in tasks)
from btc35 import app, db_init, load_signals

# Initialize database
try:
    db_init()
    load_signals()
    print("[WSGI-PA] Database initialized and signals loaded")
except Exception as e:
    print(f"[WSGI-PA ERROR] Initialization: {e}")

# Expose for WSGI
application = app

if __name__ == "__main__":
    application.run(host="127.0.0.1", port=5007, threaded=True)
