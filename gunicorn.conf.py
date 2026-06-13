# services/language/gunicorn.conf.py
"""
Gunicorn-Konfiguration für translate-svc.

Ziele
- Sichere Defaults, per ENV überschreibbar.
- GThread-Worker für parallele I/O (Upstream-Calls).
- JSON-Logs an stdout/stderr (root-Logger übernimmt in core.logging).
- Robust gegenüber fehlerhaften ENV-Werten.

Wichtige ENV
  PORT=8000
  HOST=0.0.0.0
  LOG_LEVEL=INFO
  WEB_CONCURRENCY=<auto>         # Worker-Anzahl
  GUNICORN_THREADS=4
  GUNICORN_TIMEOUT=60
  GUNICORN_GRACEFUL_TIMEOUT=30
  GUNICORN_KEEPALIVE=5
  GUNICORN_MAX_REQUESTS=1000
  GUNICORN_MAX_REQUESTS_JITTER=100
  GUNICORN_LIMIT_REQ_LINE=4094
  GUNICORN_LIMIT_REQ_FIELDS=100
  GUNICORN_LIMIT_REQ_FIELD_SIZE=8190
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from typing import Any

# ----------------------------- Helpers -----------------------------

def _env_int(name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    try:
        v = int(os.getenv(name, str(default)))
    except Exception:
        v = default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v

def _env_str(name: str, default: str) -> str:
    try:
        v = os.getenv(name, default)
        return v if v else default
    except Exception:
        return default

def _cpu_count() -> int:
    try:
        c = multiprocessing.cpu_count()
        return c if c and c > 0 else 1
    except Exception:
        return 1

# ----------------------------- Bind -----------------------------

_host = _env_str("HOST", "0.0.0.0")
_port = _env_int("PORT", 8000, lo=1, hi=65535)
bind = f"{_host}:{_port}"

# ----------------------------- Workers / Threads -----------------------------

# Faustregel: 2 * CPU + 1
_default_workers = 2 * _cpu_count() + 1
workers = _env_int("WEB_CONCURRENCY", _default_workers, lo=1)

# GThread für blockierende I/O (HTTP zum Upstream)
worker_class = "gthread"
threads = _env_int("GUNICORN_THREADS", 4, lo=1, hi=64)

# Vorladen spart RAM pro Worker und beschleunigt erste Requests
preload_app = True

# ----------------------------- Timeouts / Keepalive -----------------------------

timeout = _env_int("GUNICORN_TIMEOUT", 60, lo=10, hi=600)                # Hard-Timeout pro Request
graceful_timeout = _env_int("GUNICORN_GRACEFUL_TIMEOUT", 30, lo=5, hi=300)
keepalive = _env_int("GUNICORN_KEEPALIVE", 5, lo=1, hi=120)

# ----------------------------- Logging -----------------------------

# Root-Logger wird in core.logging auf JSON gesetzt.
# Gunicorn-Logs gehen an stdout/stderr und propagieren.
accesslog = "-"   # stdout
errorlog = "-"    # stderr
capture_output = True

# gunicorn loglevel (lower-case)
_loglevel = _env_str("LOG_LEVEL", "INFO").lower()
if _loglevel not in ("debug", "info", "warning", "error", "critical"):
    _loglevel = "info"
loglevel = _loglevel

# Access-Log mit X-Request-ID wenn vorhanden
# %({Header}i)s liest einen Request-Header
access_log_format = (
    '%(h)s - "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" '
    'req_id=%({X-Request-ID}i)s rt=%(L)s'
)

# ----------------------------- Stabilität -----------------------------

# Memory-Leak-Schutz
max_requests = _env_int("GUNICORN_MAX_REQUESTS", 1000, lo=100, hi=100000)
max_requests_jitter = _env_int("GUNICORN_MAX_REQUESTS_JITTER", 100, lo=0, hi=1000)

# Schutz vor Header-Exzessen
limit_request_line = _env_int("GUNICORN_LIMIT_REQ_LINE", 4094, lo=512, hi=16384)
limit_request_fields = _env_int("GUNICORN_LIMIT_REQ_FIELDS", 100, lo=10, hi=1000)
limit_request_field_size = _env_int("GUNICORN_LIMIT_REQ_FIELD_SIZE", 8190, lo=512, hi=65535)

# Weiterleitung vertrauen (Compose/Ingress)
forwarded_allow_ips = "*"
proxy_protocol = False

# Prozessname für ps/top
proc_name = "translate-svc"

# ----------------------------- Hooks -----------------------------

def when_ready(server):  # type: ignore[override]
    try:
        server.log.info("gunicorn_ready bind=%s workers=%s threads=%s", bind, workers, threads)
    except Exception:
        pass

def on_starting(server):  # type: ignore[override]
    try:
        # stdout line-buffering erzwingen
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

def post_worker_init(worker):  # type: ignore[override]
    try:
        worker.log.info("worker_started pid=%s wid=%s", worker.pid, worker.id)
    except Exception:
        pass

def worker_abort(worker):  # type: ignore[override]
    try:
        worker.log.warning("worker_abort pid=%s wid=%s", worker.pid, worker.id)
    except Exception:
        pass
