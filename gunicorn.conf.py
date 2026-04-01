# Gunicorn Configuration File
# Production WSGI server settings

import multiprocessing

# Server socket
bind = "0.0.0.0:5007"
backlog = 2048

# Worker processes
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "sync"
worker_connections = 1000
timeout = 120
keepalive = 5

# Threads
threads = 4

# Process naming
proc_name = "dashbtc"

# Server mechanics
daemon = False
pidfile = "/tmp/gunicorn.pid"
umask = 0
user = None
group = None
tmp_upload_dir = None

# Logging
errorlog = "-"
loglevel = "info"
accesslog = "-"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s"'

# Process management
max_requests = 1000
max_requests_jitter = 50
graceful_timeout = 30

# SSL (if needed)
# keyfile = None
# certfile = None
