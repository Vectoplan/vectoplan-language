# services/language/core/errors.py
"""
Zentrale Fehler- und Antwortstruktur für translate-svc.

Eigenschaften
- Einheitliche JSON-Struktur: { "error": {code, message, details}, "meta": {request_id} }
- Saubere HTTP-Statuszuordnung
- Robuste Fallbacks ohne Flask-Hard-Dependency
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Mapping, Optional, Tuple

# Flask ist optional. Module dürfen auch außerhalb eines App-Kontexts funktionieren.
try:  # pragma: no cover
    from flask import Response, jsonify, g
except Exception:  # pragma: no cover
    Response = None  # type: ignore
    jsonify = None  # type: ignore
    g = None  # type: ignore


__all__ = [
    "APIError",
    "BadRequest",
    "ValidationError",
    "Unauthorized",
    "Forbidden",
    "NotFound",
    "Conflict",
    "UnsupportedMediaType",
    "PayloadTooLarge",
    "TooManyRequests",
    "TimeoutExceeded",
    "UpstreamError",
    "NetworkError",
    "NotReady",
    "InternalServerError",
    "error_payload",
    "as_response",
    "assert_condition",
    "map_exception",
]


# ---------------------------------------------------------------------------
# Kernfehler
# ---------------------------------------------------------------------------

class APIError(Exception):
    """Basisfehler mit HTTP-Status, internem Code und Details."""

    status: int
    code: str
    message: str
    details: Dict[str, Any]

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code or "api_error")
        self.message = str(message or "")
        self.details = dict(details or {})

    def to_dict(self, *, include_meta: bool = True) -> Dict[str, Any]:
        payload = {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }
        if include_meta:
            rid = None
            try:
                rid = getattr(g, "request_id", None)  # type: ignore
            except Exception:
                rid = None
            payload["meta"] = {"request_id": rid}
        return payload

    def to_response(self) -> Tuple[Any, int]:
        """
        Gibt ein Response-kompatibles Paar zurück.
        - Mit Flask: (jsonify(payload), status)
        - Ohne Flask: (json_string, status) (der Aufrufer muss Header setzen)
        """
        data = self.to_dict(include_meta=True)
        if jsonify is not None:
            try:
                return jsonify(data), self.status  # type: ignore[return-value]
            except Exception:
                pass
        return json.dumps(data, ensure_ascii=False), self.status


# ---------------------------------------------------------------------------
# Spezifische Fehlerklassen
# ---------------------------------------------------------------------------

class BadRequest(APIError):
    def __init__(self, message: str = "bad_request", details: Optional[Mapping[str, Any]] = None):
        super().__init__(400, "bad_request", message, details)


class ValidationError(APIError):
    def __init__(self, message: str = "invalid_argument", details: Optional[Mapping[str, Any]] = None):
        super().__init__(400, "invalid_argument", message, details)


class Unauthorized(APIError):
    def __init__(self, message: str = "unauthorized", details: Optional[Mapping[str, Any]] = None):
        super().__init__(401, "unauthorized", message, details)


class Forbidden(APIError):
    def __init__(self, message: str = "forbidden", details: Optional[Mapping[str, Any]] = None):
        super().__init__(403, "forbidden", message, details)


class NotFound(APIError):
    def __init__(self, message: str = "not_found", details: Optional[Mapping[str, Any]] = None):
        super().__init__(404, "not_found", message, details)


class Conflict(APIError):
    def __init__(self, message: str = "conflict", details: Optional[Mapping[str, Any]] = None):
        super().__init__(409, "conflict", message, details)


class UnsupportedMediaType(APIError):
    def __init__(self, message: str = "unsupported_media_type", details: Optional[Mapping[str, Any]] = None):
        super().__init__(415, "unsupported_media_type", message, details)


class PayloadTooLarge(APIError):
    def __init__(self, message: str = "payload_too_large", details: Optional[Mapping[str, Any]] = None):
        super().__init__(413, "payload_too_large", message, details)


class TooManyRequests(APIError):
    def __init__(self, message: str = "rate_limited", details: Optional[Mapping[str, Any]] = None):
        super().__init__(429, "rate_limited", message, details)


class TimeoutExceeded(APIError):
    def __init__(self, message: str = "timeout", details: Optional[Mapping[str, Any]] = None):
        super().__init__(504, "timeout", message, details)


class UpstreamError(APIError):
    def __init__(self, status: int = 502, message: str = "upstream_error", details: Optional[Mapping[str, Any]] = None):
        super().__init__(status or 502, "upstream_error", message, details)


class NetworkError(APIError):
    def __init__(self, message: str = "network_error", details: Optional[Mapping[str, Any]] = None):
        # 599 = synthetischer Netzfehlerstatus
        super().__init__(599, "network_error", message, details)


class NotReady(APIError):
    def __init__(self, message: str = "not_ready", details: Optional[Mapping[str, Any]] = None):
        super().__init__(503, "not_ready", message, details)


class InternalServerError(APIError):
    def __init__(self, message: str = "internal_error", details: Optional[Mapping[str, Any]] = None):
        super().__init__(500, "internal_error", message, details)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def error_payload(code: str, message: str, *, details: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Rohes Fehlerobjekt für konsistente Antworten."""
    rid = None
    try:
        rid = getattr(g, "request_id", None)  # type: ignore
    except Exception:
        rid = None
    return {
        "error": {"code": code, "message": message, "details": dict(details or {})},
        "meta": {"request_id": rid},
    }


def as_response(err: APIError) -> Tuple[Any, int]:
    """Konvertiert APIError in Response-kompatibles Paar."""
    try:
        return err.to_response()
    except Exception:  # pragma: no cover
        logging.getLogger(__name__).exception("error_to_response_failed")
        fallback = error_payload("internal_error", "Unerwarteter Fehler.")
        return json.dumps(fallback, ensure_ascii=False), 500


def assert_condition(condition: bool, *, code: str, message: str, status: int = 400, details: Optional[Mapping[str, Any]] = None) -> None:
    """Wirft APIError, falls Bedingung False ist."""
    if not condition:
        raise APIError(status, code, message, details)


def map_exception(exc: Exception) -> APIError:
    """
    Mappt beliebige Exceptions defensiv auf APIError.
    - Übernimmt vorhandene Felder status/code/message, wenn vorhanden.
    - Erkennt einige Standardfehler.
    """
    if isinstance(exc, APIError):
        return exc

    # HttpError aus clients.http erkennen ohne direkte Import-Abhängigkeit
    status = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    details = getattr(exc, "details", None)

    if isinstance(status, int) and status > 0 and isinstance(code, str):
        return APIError(status, code, message, details)

    # Standard-Python-Fehler heuristisch abbilden
    if isinstance(exc, ValueError):
        return ValidationError(str(exc))
    if isinstance(exc, KeyError):
        return BadRequest(f"missing_key:{exc!s}")
    if isinstance(exc, TimeoutError):  # noqa: F823 (built-in)
        return TimeoutExceeded(str(exc))

    # Fallback
    logging.getLogger(__name__).exception("unmapped_exception")
    return InternalServerError("Unerwarteter Fehler.", details={"type": type(exc).__name__})
