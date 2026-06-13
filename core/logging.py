# services/language/core/logging.py
"""
Strukturiertes JSON-Logging für translate-svc.

Eigenschaften
-------------
- Ein Handler auf stdout.
- Request-ID aus Flask-g oder Kontext (ContextVar), falls vorhanden.
- Kontextfelder via ContextVar (_LOG_CTX).
- Sichere Defaults ohne externe Libs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

# Thread-/Greenlet-sicherer Kontext für Zusatzfelder
_LOG_CTX: ContextVar[Dict[str, Any]] = ContextVar("_LOG_CTX", default={})

# Bekannte LogRecord-Felder (nicht erneut serialisieren)
_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
}


def _iso_ts() -> str:
    """
    Liefert einen ISO-8601-Timestamp in UTC, Millisekunden-genau.
    """
    try:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:  # pragma: no cover
        # Fallback: time.time() in Sekunden
        return str(int(time.time()))


def _get_request_id_from_flask() -> Optional[str]:
    """
    Versucht eine Request-ID aus Flask.g zu lesen.
    """
    try:
        from flask import g  # type: ignore

        rid = getattr(g, "request_id", None)
        return str(rid) if rid else None
    except Exception:
        return None


def _get_request_id_from_ctx() -> Optional[str]:
    """
    Versucht eine Request-ID aus dem Logging-Kontext (_LOG_CTX) zu lesen.
    """
    try:
        ctx = _LOG_CTX.get()
        rid = ctx.get("request_id")
        return str(rid) if rid else None
    except Exception:
        return None


def _get_request_id(record: logging.LogRecord) -> Optional[str]:
    """
    Ermittelt Request-ID in folgender Priorität:
      1. Flask.g.request_id
      2. ContextVar _LOG_CTX["request_id"]
      3. record.request_id / record.correlation_id
    """
    rid = _get_request_id_from_flask()
    if rid:
        return rid
    rid = _get_request_id_from_ctx()
    if rid:
        return rid
    try:
        rid_attr = getattr(record, "request_id", None) or getattr(record, "correlation_id", None)
        return str(rid_attr) if rid_attr else None
    except Exception:
        return None


def _jsonify_value(val: Any) -> Any:
    """
    Robuste, verlustarme Serialisierung für JSON-Felder.
    """
    try:
        if val is None or isinstance(val, (bool, int, float, str)):
            return val
        if isinstance(val, (list, tuple)):
            return [_jsonify_value(x) for x in val]
        if isinstance(val, dict):
            return {str(k): _jsonify_value(v) for k, v in val.items()}
        # Fallback auf str()
        return str(val)
    except Exception:
        return "<unserializable>"


def _record_extras(record: logging.LogRecord) -> Dict[str, Any]:
    """
    Extrahiert nicht-reservierte Felder aus dem LogRecord als JSON-kompatibles Dict.
    """
    out: Dict[str, Any] = {}
    try:
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_"):
                continue
            # einige Standardfelder filtern
            if k in ("asctime", "message"):
                continue
            out[k] = _jsonify_value(v)
    except Exception:
        # niemals Logging sprengen
        pass
    return out


class JsonFormatter(logging.Formatter):
    """
    JSON-Formatter für strukturierte Logs.

    Optionen:
      - pretty:   Mehrzeilig mit Einrückung (für Dev).
      - ensure_ascii: True, um Nicht-ASCII-Zeichen zu escapen.
    """

    def __init__(self, *, pretty: bool = False, ensure_ascii: bool = False):
        super().__init__()
        self.pretty = bool(pretty)
        self.ensure_ascii = bool(ensure_ascii)

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": _iso_ts(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "src": f"{record.filename}:{record.lineno}",
        }

        # Request-ID aus Flask, ContextVar oder LogRecord
        rid = _get_request_id(record)
        if rid:
            payload["request_id"] = rid

        # Kontextfelder mergen
        try:
            ctx = dict(_LOG_CTX.get())
            if ctx:
                for k, v in ctx.items():
                    if k not in payload:
                        payload[k] = _jsonify_value(v)
        except Exception:
            # Kontext darf Logging nicht zerstören
            pass

        # Extras aus dem LogRecord aufnehmen
        extras = _record_extras(record)
        for k, v in extras.items():
            if k not in payload:
                payload[k] = v

        # Exception-Info anfügen
        if record.exc_info:
            try:
                payload["exc"] = self.formatException(record.exc_info)
            except Exception:
                payload["exc"] = "exception"

        # JSON-Serialisierung
        try:
            if self.pretty:
                return json.dumps(payload, ensure_ascii=self.ensure_ascii, indent=2)
            return json.dumps(payload, ensure_ascii=self.ensure_ascii, separators=(",", ":"))
        except Exception:
            # Letzte Absicherung
            return (
                '{"ts":"%s","level":"ERROR","logger":"logging","msg":"json_serialization_failed"}'
                % _iso_ts()
            )


class RequestIdFilter(logging.Filter):
    """
    Filter, der sicherstellt, dass eine request_id (falls vorhanden) am Record hängt.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rid = _get_request_id(record)
            if rid and not hasattr(record, "request_id"):
                setattr(record, "request_id", rid)
        except Exception:
            pass
        return True


def setup_logging(level: str = "INFO") -> None:
    """
    Setzt Root-Logger auf JSON-Format. Ersetzt bestehende Handler.

    - Level: aus Parameter (oder ENV) abgeleitet.
    - Gunicorn/Requests/Werkzeug werden auf sinnvolle Level gesetzt und
      propagieren zum Root-Logger.
    """
    try:
        lvl = getattr(logging, (level or "INFO").upper(), logging.INFO)
    except Exception:
        lvl = logging.INFO

    try:
        pretty = str(os.getenv("LOG_JSON_PRETTY", "0")).strip().lower() in (
            "1",
            "true",
            "yes",
            "y",
            "on",
        )
    except Exception:
        pretty = False

    try:
        ascii_only = str(os.getenv("LOG_JSON_ASCII", "0")).strip().lower() in (
            "1",
            "true",
            "yes",
            "y",
            "on",
        )
    except Exception:
        ascii_only = False

    root = logging.getLogger()
    # Vorhandene Handler entfernen
    for h in list(root.handlers):
        try:
            root.removeHandler(h)
        except Exception:
            pass

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter(pretty=pretty, ensure_ascii=ascii_only))
    handler.addFilter(RequestIdFilter())

    root.addHandler(handler)
    root.setLevel(lvl)

    # Drittlogger beruhigen und zum Root propagieren
    for name, llevel in (
        ("werkzeug", logging.INFO),
        ("urllib3", logging.WARNING),
        ("requests", logging.WARNING),
        ("gunicorn", logging.INFO),
        ("gunicorn.error", logging.INFO),
        ("gunicorn.access", logging.INFO),
    ):
        try:
            lg = logging.getLogger(name)
            for hh in list(lg.handlers):
                lg.removeHandler(hh)
            lg.propagate = True
            lg.setLevel(llevel)
        except Exception:
            continue

    # Startmeldung
    try:
        logging.getLogger(__name__).info(
            "logging_initialized",
            extra={"log_config": {"level": level, "pretty": pretty, "ascii_only": ascii_only}},
        )
    except Exception:
        # Logging der Startmeldung ist nicht kritisch
        pass


# ---------------------------------------------------------------------------
# Kontextsteuerung
# ---------------------------------------------------------------------------


def bind_context(**fields: Any) -> None:
    """
    Fügt Kontextfelder hinzu oder überschreibt sie.

    Typische Felder:
      - request_id
      - client_ip
      - client_id
      - method
      - path
    """
    try:
        ctx = dict(_LOG_CTX.get())
        for k, v in fields.items():
            if v is not None:
                ctx[str(k)] = v
        _LOG_CTX.set(ctx)
    except Exception:
        # Kontextfehler sollen Logging nicht zerschießen
        pass


def unbind_context(*keys: str) -> None:
    """
    Entfernt bestimmte Kontextfelder.
    """
    try:
        ctx = dict(_LOG_CTX.get())
        for k in keys:
            ctx.pop(k, None)
        _LOG_CTX.set(ctx)
    except Exception:
        pass


def clear_context() -> None:
    """
    Löscht alle Kontextfelder.
    """
    try:
        _LOG_CTX.set({})
    except Exception:
        pass


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """
    Bequemer Zugriff auf Logger, mit robustem Fallback.
    """
    try:
        return logging.getLogger(name or __name__)
    except Exception:  # pragma: no cover
        return logging.getLogger(__name__)
