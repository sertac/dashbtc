"""
Gunicorn config — post_worker_init ile background loop başlat
"""
import threading

def _log(msg):
    print(msg, flush=True)

def post_worker_init(worker):
    """Her worker başlatıldığında background loop'u başlat."""
    _log(f"[GUNICORN] post_worker_init: worker {worker.pid}")
    try:
        from btc35 import background_loop

        # Background loop thread (daemon — worker ölünce biter)
        def _run():
            try:
                _log(f"[BG THREAD {worker.pid}] Starting background_loop...")
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
