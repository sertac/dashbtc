"""
Gunicorn config — post_worker_init ile background loop başlat
"""
import threading

def post_worker_init(worker):
    """Her worker başlatıldığında background loop'u başlat."""
    from btc35 import background_loop, db_init, load_signals
    import sys
    import os

    def _log(msg):
        print(msg, flush=True)

    def _run():
        try:
            _log(f"[WORKER {os.getpid()}] Background loop started")
            background_loop()
        except Exception as e:
            _log(f"[WORKER {os.getpid()}] BG LOOP CRASH: {e}")
            import traceback
            _log(traceback.format_exc())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    _log(f"[WORKER {os.getpid()}] Background loop thread started")
