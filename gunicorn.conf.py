"""
Gunicorn config — post_worker_init ile background loop başlat
"""
import threading
import time

def _log(msg):
    print(msg, flush=True)

_bg_started = {}

def post_worker_init(worker):
    """Her worker başlatıldığında background loop'u başlat."""
    _log(f"[GUNICORN] post_worker_init: worker {worker.pid}")
    
    # Import'u geciktirme — worker thread'i bloklamamak için
    def _start_bg():
        try:
            from btc35 import background_loop, db_init, load_signals
            
            # DB init (ayrı thread'de)
            try:
                db_init()
                load_signals()
                _log("[GUNICORN] DB init OK")
            except Exception as e:
                _log(f"[GUNICORN] DB init ERR: {e}")
            
            # Background loop (daemon thread)
            t = threading.Thread(target=background_loop, daemon=True)
            t.start()
            _log(f"[GUNICORN] Background thread started (alive={t.is_alive()})")
            _bg_started[worker.pid] = True
        except Exception as e:
            _log(f"[GUNICORN] post_worker_init ERR: {e}")
            import traceback
            _log(traceback.format_exc())
    
    # Hemen geri dön — worker bloklanmaz
    t = threading.Thread(target=_start_bg, daemon=True)
    t.start()
