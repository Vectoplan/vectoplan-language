# services/language/middleware/request_context.py
"""
Request-Kontext für translate-svc.

Ziele
------
- Stabile Request-ID und Dauermessung via ContextVar.
- Extrahiert Client-Infos (IP, X-Client-ID, User-Agent) sicher.
- Bindet Kontext an core.logging (falls vorhanden).
- Robuste Fallbacks ohne harte Flask-Abhängigkeit.
- Optional: Hilfsfunktionen für Rate-Limit-Schlüssel und Outbound-Header.

Nutzung
------
- app.py ruft attach_context(request_id) im before_request auf.
- after_request kann annotate_response(resp) nutzen.
- teardown_request sollte clear() aufrufen, um Kontext zu reinigen.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

# Flask ist optional
try:  # pragma: no cover
    from flask import g, request
except Exception:  # pragma: no cover
    g = None  # type: ignore
    request = None  # type: ignore

# core.logging ist optional
try:  # pragma: no cover
    from core.logging import bind_context, clear_context as log_clear_context
except Exception:  # pragma: no cover

    def bind_context(**fields: Any) -> None:  # type: ignore[override]
        return

    def log_clear_context() -> None:  # type: ignore[override]
        return


# -----------------------------------------------------------------------------
# ContextVars
# -----------------------------------------------------------------------------

_REQUEST_ID: ContextVar[Optional[str]] = ContextVar("_REQUEST_ID", default=None)
_STARTED_AT: ContextVar[Optional[float]] = ContextVar("_STARTED_AT", default=None)
_CTX: ContextVar[Dict[str, Any]] = ContextVar("_CTX", default={})

_LOG = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Interna
# -----------------------------------------------------------------------------

def _now() -> float:
    """Monotone Zeitquelle (perf_counter, Fallback time.time)."""
    try:
        return time.perf_counter()
    except Exception:
        return time.time()


def _new_id() -> str:
    """Erzeugt eine neue Request-ID."""
    try:
        return str(uuid.uuid4())
    except Exception:  # pragma: no cover
        return str(int(time.time() * 1000))


def _extract_ip() -> Optional[str]:
    """
    Extrahiert die Client-IP so robust wie möglich.

    Reihenfolge:
      - X-Forwarded-For (erstes Element)
      - X-Real-IP
      - request.remote_addr
    """
    try:
        if request is None:
            return None
        xf = (request.headers.get("X-Forwarded-For") or "").strip()
        if xf:
            # Erstes Element der Kette
            first = xf.split(",")[0].strip()
            if first:
                return first
        xr = (request.headers.get("X-Real-IP") or "").strip()
        if xr:
            return xr
        ra = getattr(request, "remote_addr", None)
        return ra.strip() if isinstance(ra, str) and ra else None
    except Exception:
        return None


def _extract_user_agent() -> Optional[str]:
    """Extrahiert User-Agent (max. 256 Zeichen)."""
    try:
        if request is None:
            return None
        ua = request.headers.get("User-Agent")
        if ua:
            return ua[:256]
        return None
    except Exception:
        return None


def _extract_client_id() -> Optional[str]:
    """Liest eine optionale Client-ID aus X-Client-ID/X-Client-Id."""
    try:
        if request is None:
            return None
        cid = request.headers.get("X-Client-ID") or request.headers.get("X-Client-Id")
        return cid.strip() if cid else None
    except Exception:
        return None


def _extract_host() -> Optional[str]:
    """Extrahiert den Host-Header, falls vorhanden."""
    try:
        if request is None:
            return None
        host = request.headers.get("Host")
        return host.strip() if host else None
    except Exception:
        return None


# -----------------------------------------------------------------------------
# API
# -----------------------------------------------------------------------------

def attach_context(request_id: Optional[str] = None) -> str:
    """
    Initialisiert Request-Kontext.

    - Ermittelt/übernimmt Request-ID (Header X-Request-ID oder Argument).
    - Speichert Startzeit (für elapsed_ms).
    - Extrahiert Client-IP, Client-ID, User-Agent, Path, Method.
    - Bindet alle relevanten Felder an core.logging (bind_context).
    - Hinterlegt Request-ID und Startzeit zusätzlich in Flask.g (falls vorhanden).
    """
    rid = request_id
    try:
        if not rid and request is not None:
            rid = request.headers.get("X-Request-ID")
    except Exception:
        rid = None
    if not rid:
        rid = _new_id()

    _REQUEST_ID.set(rid)
    _STARTED_AT.set(_now())

    path: Optional[str] = None
    method: Optional[str] = None
    query: Optional[str] = None
    try:
        if request is not None:
            path = request.path
            method = request.method
            query = request.query_string.decode("utf-8", errors="ignore") or None
    except Exception:
        path = path or None
        method = method or None
        query = query or None

    ip = _extract_ip()
    cid = _extract_client_id()
    ua = _extract_user_agent()
    host = _extract_host()

    ctx: Dict[str, Any] = {
        "request_id": rid,
        "client_ip": ip,
        "client_id": cid,
        "method": method,
        "path": path,
        "query": query,
        "user_agent": ua,
        "host": host,
    }
    # Nur nicht-leere Felder speichern
    try:
        _CTX.set({k: v for k, v in ctx.items() if v is not None})
    except Exception:
        _CTX.set({})

    # Flask g
    try:
        if g is not None:
            g.request_id = rid
            g.request_started_at = _STARTED_AT.get()
            g.client_ip = ip
            g.client_id = cid
    except Exception:
        pass

    # Logging-Kontext
    try:
        bind_context(
            request_id=rid,
            client_ip=ip,
            client_id=cid,
            method=method,
            path=path,
            host=host,
        )
    except Exception:
        pass

    _LOG.debug("request_context_attached")
    return rid


def context_dict() -> Dict[str, Any]:
    """Liefert den aktuellen Kontext als Dict (für Debug/Tests)."""
    try:
        return dict(_CTX.get())
    except Exception:
        return {}


def get_request_id() -> Optional[str]:
    """Liefert die aktuelle Request-ID oder None."""
    try:
        return _REQUEST_ID.get()
    except Exception:
        return None


def elapsed_ms(default: int = 0) -> int:
    """Verstrichene Zeit in Millisekunden seit attach_context()."""
    try:
        started = _STARTED_AT.get()
        if not started:
            return int(default)
        return int((_now() - float(started)) * 1000)
    except Exception:
        return int(default)


def annotate_response(resp) -> None:
    """
    Setzt X-Request-ID und X-Process-Time-ms, ohne Exceptions zu werfen.

    Hinweis:
      - app.py kann dies im after_request-Handler nutzen.
      - resp sollte ein Flask-Response-Objekt sein (hat headers-Attribut).
    """
    try:
        rid = get_request_id()
        if rid:
            try:
                resp.headers.setdefault("X-Request-ID", rid)
            except Exception:
                # resp könnte ein primitiver Typ sein; dann ignorieren
                pass
        pt = elapsed_ms()
        if pt > 0:
            try:
                resp.headers.setdefault("X-Process-Time-ms", str(pt))
            except Exception:
                pass
    except Exception:
        # Response-Modifikation darf keinen Fehler auslösen
        pass


def outbound_headers(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Liefert Header, die an Downstream-Requests (z. B. LibreTranslate) weitergegeben werden können.

    Aktuell:
      - X-Request-ID (falls vorhanden)

    Weitere Felder können später ergänzt werden.
    """
    try:
        out = dict(base or {})
        rid = get_request_id()
        if rid:
            out.setdefault("X-Request-ID", rid)
        return out
    except Exception:
        # Niemals Ausnahmen nach außen geben
        return dict(base or {})


def rate_limit_key(route: Optional[str] = None) -> str:
    """
    Bildet einen robusten Schlüssel für Rate-Limiting.

    Strategie:
      - Nutzt core.ratelimit.build_key, sofern verfügbar.
      - Fällt bei Fehlern auf statische Bildung zurück.
        Format: "ip:{ip}|client:{client_id}|route:{route}"
    """
    try:
        path = route
        if path is None and request is not None:
            path = request.path
        ip = _extract_ip() or "unknown"
        cid = _extract_client_id() or None
        try:
            from core.ratelimit import build_key  # type: ignore

            return build_key(ip, cid, path or "/")
        except Exception:
            pass
        return f"ip:{ip}|client:{cid or 'anon'}|route:{path or '/'}"
    except Exception:
        return "ip:unknown|client:anon|route:/"


def clear() -> None:
    """
    Löscht alle kontextuellen Informationen sicher.

    - Resettet ContextVars.
    - Leert Logging-Kontext via core.logging.clear_context.
    """
    try:
        _REQUEST_ID.set(None)
        _STARTED_AT.set(None)
        _CTX.set({})
        log_clear_context()
    except Exception:
        pass
