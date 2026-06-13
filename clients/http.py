# services/language/clients/http.py
"""
Robuster HTTP-Client mit optionalem requests-Backend und urllib-Fallback.

Eigenschaften
- Pooled Session (wenn requests vorhanden)
- Retries mit Exponential-Backoff und Jitter für 408/425/429/5xx/599
- Zeitlimits, sichere JSON-Verarbeitung, Limit für Antwortgröße
- Schlanke API: request(), get_json(), post_json()
- Übergibt Korrelation per X-Request-ID, akzeptiert API-Key-Header

Wichtig
- JSON-Parser akzeptiert Top-Level-Objekte **und** -Listen.
- HttpError ist kompatibel zu core.errors.APIError (inkl. to_dict(include_meta=...)).
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlencode, urljoin

try:  # optional
    import requests  # type: ignore
except Exception:  # pragma: no cover
    requests = None  # type: ignore

# ---------------------------------------------------------------------------
# Fehlerklasse mit kompatibler Super-Aufruf-Logik
# ---------------------------------------------------------------------------

_APIErrorBase = Exception
try:  # optional: wenn vorhanden, daran anlehnen
    from core.errors import APIError as _APIErrorBase  # type: ignore[attr-defined]
except Exception:
    # Kein spezieller APIError vorhanden, wir bleiben bei Exception als Basis
    pass


class HttpError(_APIErrorBase):  # type: ignore[misc]
    """
    Vereinheitlichte Fehlerklasse für HTTP-/Netzwerkfehler.

    - Wenn core.errors.APIError vorhanden ist, wird diese Klasse davon abgeleitet
      und verhält sich wie ein normaler APIError (inkl. to_dict(include_meta=...)).
    - Andernfalls wird von Exception geerbt und ein einfaches Dict mit den Feldern
      status/code/message/details erzeugt.

    Attribute
    ---------
    status : int
        HTTP-ähnlicher Statuscode (z. B. 502, 504, 599).
    code : str
        Interner Fehlercode (z. B. "upstream_error", "network_error").
    message : str
        Menschlich lesbare Kurzbeschreibung, z. B. "network_error:ConnectionError".
    details : dict
        Zusätzliche Detailinformationen (z. B. url, method, bytes).
    """

    def __init__(
        self,
        status: int,
        message: str,
        *,
        code: str = "http_error",
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.status = int(status)
        self.code = str(code or "http_error")
        self.message = str(message)
        self.details = dict(details or {})
        # Versuche, die Basisklasse (APIError oder Exception) sinnvoll zu initialisieren.
        try:
            # core.errors.APIError erwartet: (status, code, message, details)
            super().__init__(self.status, self.code, self.message, self.details)  # type: ignore[misc]
        except TypeError:
            try:
                # Fallback: (status, code, message)
                super().__init__(self.status, self.code, self.message)  # type: ignore[misc]
            except TypeError:
                # Minimaler Fallback: nur message
                super().__init__(self.message)

    def to_dict(self, *, include_meta: bool = True) -> Dict[str, Any]:
        """
        Liefert ein Fehler-Payload.

        Wenn core.errors.APIError vorhanden ist, wird dessen to_dict() genutzt,
        sodass die Struktur konsistent bleibt:

            {
              "error": { "code": ..., "message": ..., "details": {...} },
              "meta":  { "request_id": ... }
            }

        Andernfalls wird ein einfaches, aber ähnliches Dict erzeugt.
        """
        # bevorzugt die Implementierung der Basisklasse nutzen, falls vorhanden
        try:
            base_to_dict = getattr(super(), "to_dict", None)  # type: ignore[attr-defined]
            if callable(base_to_dict):
                try:
                    return base_to_dict(include_meta=include_meta)  # type: ignore[call-arg]
                except TypeError:
                    # Fällt durch, wenn Basismethode kein include_meta akzeptiert
                    pass
        except Exception:
            # Niemals Exceptions beim Serialisieren nach außen geben
            logging.getLogger(__name__).warning("http_error_to_dict_super_failed", exc_info=True)

        # Fallback: manuelle Struktur, angelehnt an core.errors.APIError
        payload: Dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": dict(self.details or {}),
            }
        }
        if include_meta:
            # try best-effort, request_id aus Flask g zu lesen
            rid = None
            try:
                from flask import g  # type: ignore

                rid = getattr(g, "request_id", None)
            except Exception:
                rid = None
            payload["meta"] = {"request_id": rid}
        return payload

    def __str__(self) -> str:  # pragma: no cover - einfache Darstellung
        return f"HttpError(status={self.status}, code={self.code}, message={self.message})"

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"HttpError(status={self.status!r}, code={self.code!r}, "
            f"message={self.message!r}, details={self.details!r})"
        )


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SEC: int = 30
DEFAULT_MAX_BYTES: int = 4 * 1024 * 1024  # 4 MiB Hard-Limit je Antwort
DEFAULT_RETRIES: int = 3
DEFAULT_BACKOFF_BASE: float = 0.2  # Sekunden
DEFAULT_BACKOFF_MAX: float = 3.0   # Max Sleep pro Versuch
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504, 599}

JSONValue = Union[None, bool, int, float, str, Dict[str, Any], List[Any]]
JSONType = Union[Dict[str, Any], List[Any]]


@dataclass
class HttpClientConfig:
    base_url: str
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    max_response_bytes: int = DEFAULT_MAX_BYTES
    default_headers: Optional[Mapping[str, str]] = None
    api_key_header: Optional[str] = None       # z. B. "X-API-Key"
    api_key_value: Optional[str] = None
    verify_tls: bool = True
    retries: int = DEFAULT_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_max: float = DEFAULT_BACKOFF_MAX


# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _req_id() -> str:
    try:
        from flask import g  # type: ignore

        rid = getattr(g, "request_id", None)
        if rid:
            return str(rid)
    except Exception:
        pass
    return str(uuid.uuid4())


def _merge_headers(base: Optional[Mapping[str, str]], extra: Optional[Mapping[str, str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if base:
        out.update({k: v for k, v in base.items() if v is not None})
    if extra:
        out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _retry_after_seconds(headers: Mapping[str, str]) -> Optional[float]:
    """
    Unterstützt Sekunden oder HTTP-Date (RFC 7231).
    """
    try:
        ra = headers.get("Retry-After") or headers.get("retry-after")  # type: ignore
        if not ra:
            return None
        ra = ra.strip()
        if ra.is_numeric():  # type: ignore[attr-defined]
            return float(ra)
        dt = parsedate_to_datetime(ra)
        if isinstance(dt, datetime):
            return max(0.0, (dt - datetime.utcnow()).total_seconds())
        return None
    except Exception:
        return None


def _sleep(backoff_base: float, attempt: int, backoff_max: float, hinted: Optional[float] = None) -> None:
    base = hinted if hinted is not None else backoff_base * (2 ** attempt)
    base = max(0.0, min(backoff_max, base)) * random.uniform(0.5, 1.5)
    time.sleep(base)


def _safe_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/") + "/"
    p = path.lstrip("/")
    return urljoin(base, p)


def _looks_like_json_ctype(ctype: str) -> bool:
    try:
        c = (ctype or "").lower()
        return "json" in c or "application/problem+json" in c
    except Exception:
        return False


def _parse_json_bytes(raw: bytes) -> Optional[JSONType]:
    """
    Versucht defensiv, JSON aus Bytes zu parsen. Akzeptiert Dict **und** List.
    """
    try:
        if not raw:
            return {}
        # kleiner Shortcut: nur wenn es wie JSON aussieht
        s = raw.lstrip()
        if not s:
            return {}
        if s[:1] not in (b"{", b"["):
            # kein harter Ausschluss: wir versuchen trotzdem
            pass
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP-Client
# ---------------------------------------------------------------------------

class HttpClient:
    """
    Kontextmanager-fähiger HTTP-Client mit optionaler requests-Session und Fallback auf urllib.

    Beispiel:
        cfg = HttpClientConfig(base_url="http://example.com", timeout_sec=5)
        with HttpClient(cfg) as http:
            data, status, headers = http.get_json("/languages")
    """

    def __init__(self, config: HttpClientConfig):
        self.cfg = config
        self._session = None
        self._logger = logging.getLogger(__name__)

        if requests is not None:
            try:
                self._session = requests.Session()  # type: ignore
                self._session.headers.update({"User-Agent": "translate-svc/1.0 (+python)"})
            except Exception:  # pragma: no cover
                self._session = None

        self._base_headers: Dict[str, str] = {"Accept": "application/json"}
        if config.default_headers:
            self._base_headers.update({k: v for k, v in config.default_headers.items() if v})

        if config.api_key_header and config.api_key_value:
            self._base_headers[config.api_key_header] = str(config.api_key_value)

    # ---------------- Context API ----------------

    def close(self) -> None:
        try:
            if self._session is not None:
                self._session.close()  # type: ignore[call-arg]
        except Exception:
            # niemals Fehler nach außen leaken
            pass

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------------- High-Level JSON ----------------

    def get_json(
        self,
        path: str,
        *,
        params: Optional[Mapping[str, Union[str, int, float]]] = None,
        headers: Optional[Mapping[str, str]] = None,
        expected: Sequence[int] = (200,),
        timeout_sec: Optional[int] = None,
    ) -> Tuple[JSONType, int, Dict[str, str]]:
        body, status, resp_headers = self.request(
            "GET", path, params=params, headers=headers, expected=expected, timeout_sec=timeout_sec
        )
        if isinstance(body, (dict, list)):
            return body, status, resp_headers
        if isinstance(body, (bytes, bytearray)):
            parsed = _parse_json_bytes(bytes(body))
            return (parsed if isinstance(parsed, (dict, list)) else {}), status, resp_headers
        # Unerwarteter Typ → leeres Objekt
        return {}, status, resp_headers

    def post_json(
        self,
        path: str,
        *,
        json_body: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        expected: Sequence[int] = (200,),
        timeout_sec: Optional[int] = None,
    ) -> Tuple[JSONType, int, Dict[str, str]]:
        merged = _merge_headers({"Content-Type": "application/json"}, headers)
        body, status, resp_headers = self.request(
            "POST", path, json_body=json_body, headers=merged, expected=expected, timeout_sec=timeout_sec
        )
        if isinstance(body, (dict, list)):
            return body, status, resp_headers
        if isinstance(body, (bytes, bytearray)):
            parsed = _parse_json_bytes(bytes(body))
            return (parsed if isinstance(parsed, (dict, list)) else {}), status, resp_headers
        return {}, status, resp_headers

    # ---------------- Core request ----------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Union[str, int, float]]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        data: Optional[Union[str, bytes]] = None,
        headers: Optional[Mapping[str, str]] = None,
        expected: Sequence[int] = (200,),
        timeout_sec: Optional[int] = None,
    ) -> Tuple[Union[bytes, JSONType], int, Dict[str, str]]:
        """
        Führt einen HTTP-Request aus und gibt (body, status, headers) zurück.

        body:
          - bytes, wenn kein JSON erkannt wurde
          - dict/list, wenn JSON erfolgreich geparst wurde

        Verhalten:
          - Bei unerwarteten Statuscodes → HttpError(upstream_error)
          - Bei Netzwerk-/Sonstfehlern nach Retries → HttpError(network_error)
        """
        url = _safe_url(self.cfg.base_url, path)
        hdrs = _merge_headers(self._base_headers, headers)
        hdrs["X-Request-ID"] = hdrs.get("X-Request-ID") or _req_id()

        t_start = _now_ms()
        timeout = int(timeout_sec or self.cfg.timeout_sec)
        retries = max(0, self.cfg.retries)
        expected_set = set(expected or (200,))
        attempt = 0

        self._logger.debug("http_request_start method=%s url=%s params=%s attempt=%s", method, url, params, attempt)

        while True:
            try:
                if self._session is not None:
                    body, status, resp_headers = self._do_requests_request(
                        self._session,
                        method,
                        url,
                        params=params,
                        json_body=json_body,
                        data=data,
                        headers=hdrs,
                        timeout=timeout,
                    )
                else:
                    body, status, resp_headers = self._do_urllib_request(
                        method,
                        url,
                        params=params,
                        json_body=json_body,
                        data=data,
                        headers=hdrs,
                        timeout=timeout,
                    )

                if status in expected_set:
                    dur = _now_ms() - t_start
                    self._logger.debug("http_request_success status=%s dur_ms=%s url=%s", status, dur, url)
                    return body, status, resp_headers

                if status in RETRY_STATUSES and attempt < retries:
                    hinted = _retry_after_seconds(resp_headers)
                    self._logger.warning(
                        "http_retry status=%s attempt=%s/%s hinted=%s", status, attempt + 1, retries, hinted
                    )
                    _sleep(self.cfg.backoff_base, attempt, self.cfg.backoff_max, hinted)
                    attempt += 1
                    continue

                raise HttpError(status, f"unexpected_status_{status}", code="upstream_error", details={"url": url})

            except HttpError:
                # bereits gemappter HttpError → direkt weiterreichen
                raise
            except Exception as exc:
                if attempt < retries:
                    self._logger.warning(
                        "http_exception_retry attempt=%s/%s err=%s",
                        attempt + 1,
                        retries,
                        type(exc).__name__,
                    )
                    _sleep(self.cfg.backoff_base, attempt, self.cfg.backoff_max, None)
                    attempt += 1
                    continue
                msg = f"network_error:{type(exc).__name__}"
                raise HttpError(599, msg, code="network_error", details={"url": url, "method": method}) from exc

    # ---------------- Backends ----------------

    def _do_requests_request(
        self,
        session: "requests.Session",  # type: ignore[name-defined]
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Union[str, int, float]]],
        json_body: Optional[Mapping[str, Any]],
        data: Optional[Union[str, bytes]],
        headers: Mapping[str, str],
        timeout: int,
    ) -> Tuple[Union[bytes, JSONType], int, Dict[str, str]]:
        assert requests is not None  # nur für Typchecker
        kwargs: Dict[str, Any] = {
            "method": method.upper(),
            "url": url,
            "headers": dict(headers),
            "timeout": timeout,
            "verify": self.cfg.verify_tls,
        }
        if params:
            kwargs["params"] = params
        if json_body is not None:
            kwargs["json"] = json_body
        elif data is not None:
            kwargs["data"] = data

        started = time.perf_counter()
        resp = session.request(**kwargs)  # type: ignore[arg-type]
        dur_ms = int((time.perf_counter() - started) * 1000)
        self._logger.debug("requests_request_done dur_ms=%s url=%s status=%s", dur_ms, url, resp.status_code)

        content: bytes = resp.content or b""
        size = len(content)
        max_bytes = max(1, int(self.cfg.max_response_bytes))
        if size > max_bytes:
            self._logger.warning("response_too_large bytes=%s max=%s url=%s", size, max_bytes, url)
            raise HttpError(502, "response_too_large", code="upstream_truncated", details={"bytes": size})

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if _looks_like_json_ctype(ctype):
            try:
                data_obj = resp.json()
                if isinstance(data_obj, (dict, list)):
                    return data_obj, resp.status_code, dict(resp.headers)
            except Exception:
                pass
        # Fallback: versuchen, Bytes als JSON zu parsen
        parsed = _parse_json_bytes(content)
        if isinstance(parsed, (dict, list)):
            return parsed, resp.status_code, dict(resp.headers)
        return content, resp.status_code, dict(resp.headers)

    def _do_urllib_request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Mapping[str, Union[str, int, float]]],
        json_body: Optional[Mapping[str, Any]],
        data: Optional[Union[str, bytes]],
        headers: Mapping[str, str],
        timeout: int,
    ) -> Tuple[Union[bytes, JSONType], int, Dict[str, str]]:
        from urllib.request import Request, urlopen  # type: ignore
        from urllib.error import HTTPError as UrlHTTPError, URLError  # type: ignore

        if params:
            qs = urlencode({k: str(v) for k, v in params.items()}, doseq=True)
            url = url + ("&" if ("?" in url) else "?") + qs

        payload: Optional[bytes] = None
        hdrs = dict(headers)

        if json_body is not None:
            try:
                payload = json.dumps(json_body).encode("utf-8")
            except Exception:
                payload = b"{}"
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            payload = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray)):
            payload = bytes(data)

        req = Request(url=url, method=method.upper(), headers=hdrs, data=payload)

        context = None
        try:
            if url.lower().startswith("https"):
                if self.cfg.verify_tls:
                    context = ssl.create_default_context()
                else:
                    context = ssl._create_unverified_context()  # nosec B323
        except Exception:
            context = None

        started = time.perf_counter()
        try:
            if context is None:
                resp = urlopen(req, timeout=timeout)  # nosec B310
            else:
                resp = urlopen(req, timeout=timeout, context=context)  # nosec B310

            try:
                raw = resp.read(max(1, int(self.cfg.max_response_bytes)) + 1)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

            if len(raw) > max(1, int(self.cfg.max_response_bytes)):
                raise HttpError(502, "response_too_large", code="upstream_truncated", details={"bytes": len(raw)})

            status = getattr(resp, "status", 200) or 200
            resp_headers: Dict[str, str] = {}
            try:
                for k in resp.headers.keys():
                    resp_headers[str(k)] = resp.headers[k]
            except Exception:
                pass

            ctype = (resp_headers.get("Content-Type") or "").lower()
            if _looks_like_json_ctype(ctype):
                parsed = _parse_json_bytes(raw)
                if isinstance(parsed, (dict, list)):
                    return parsed, status, resp_headers

            parsed = _parse_json_bytes(raw)
            if isinstance(parsed, (dict, list)):
                return parsed, status, resp_headers
            return raw, status, resp_headers

        except UrlHTTPError as e:
            status = int(getattr(e, "code", 502) or 502)
            raise HttpError(status, f"http_error_{status}", code="upstream_error", details={"url": url}) from e
        except URLError as e:
            raise HttpError(599, "network_error", code="network_error", details={"url": url}) from e
        finally:
            dur_ms = int((time.perf_counter() - started) * 1000)
            logging.getLogger(__name__).debug("urllib_request_done dur_ms=%s url=%s", dur_ms, url)
