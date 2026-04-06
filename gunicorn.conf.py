"""
Gunicorn config — post_worker_init ile background loop başlat
"""
import threading
import sys

def _log(msg):
    print(msg, flush=True)

def post_worker_init(worker):
    """Her worker başlatıldığında background loop'u başlat."""
    _log(f"[GUNICORN] post_worker_init: worker {worker.pid}")
    try:
        from btc35 import background_loop, db_init, load_signals
        _log("[GUNICORN] Import OK")

        # DB init
        try:
            db_init()
            load_signals()
            _log("[GUNICORN] DB init OK")
        except Exception as e:
            _log(f"[GUNICORN] DB init ERR: {e}")

        # Background loop thread
        def _run():
            try:
                _log(f"[BG THREAD {worker.pid}] Starting...")
                background_loop()
            except Exception as e:
                _log(f"[BG THREAD {worker.pid}] CRASH: {e}")
                import traceback
                _log(traceback.format_exc())

        t = threading.Thread(target=_run, daemon=True, name="bg-loop")
        t.start()
        _log(f"[GUNICORN] Background thread started (alive={t.is_alive()})")
    except Exception as e:
        _log(f"[GUNICORN] post_worker_init ERR: {e}")
        import traceback
        _log(traceback.format_exc())
