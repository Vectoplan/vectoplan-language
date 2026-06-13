# services/language/routes/health.py
from __future__ import annotations

import json
import logging
import time
from threading import RLock
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flask import Blueprint, Response, current_app, jsonify, request

bp = Blueprint("health", __name__)
log = logging.getLogger(__name__)

# ---------------------------
# HTTP-Backends (requests + urllib)
# ---------------------------

try:  # optional HTTP-Backend
    import requests  # type: ignore

    _HAS_REQUESTS = True
except Exception:  # pragma: no cover
    _HAS_REQUESTS = False
    requests = None  # type: ignore

# urllib-Backend immer importieren, damit wir auch bei vorhandenem requests
# sauber auf den Fallback zurückgreifen können.
from urllib.request import Request, urlopen  # type: ignore
from urllib.error import URLError  # type: ignore

# ---------------------------
# Cache für Upstream-Snapshot
# ---------------------------

_CACHE: Dict[str, object] = {"ts": 0.0, "status": None, "available": [], "latency_ms": None, "error": None}
_LOCK = RLock()


# ---------------------------
# Helpers: Config / Query
# ---------------------------


def _cfg_float(key: str, default: float) -> float:
    """
    Liest einen float aus current_app.config, mit defensiven Defaults.
    """
    try:
        val = float(current_app.config.get(key, default))
        return val if val >= 0.0 else default
    except Exception:
        return default


def _parse_bool_query(name: str, default: bool = False) -> bool:
    """
    Liest einen booleschen Query-Parameter robust aus (?param=1/true/yes/on).
    """
    try:
        raw = request.args.get(name, None)
        if raw is None:
            return default
        v = raw.strip().lower()
        return v in {"1", "true", "yes", "on"}
    except Exception:
        return default


# ---------------------------
# Helpers: Sprachcodes
# ---------------------------


def _canon_lang(code: Optional[str]) -> Optional[str]:
    """
    Kanonisiert Sprachcodes möglichst über core.languages, mit robustem Fallback.
    """
    if code is None:
        return None
    try:
        from core.languages import canon_lang as _cl  # type: ignore

        return _cl(code)
    except Exception:
        try:
            s = code.strip().replace("_", "-").lower()
        except Exception:  # pragma: no cover
            return None
        if s in ("zh", "zh-cn", "zh-hans"):
            return "zh-CN"
        if s in ("zh-tw", "zh-hant", "zh-hk"):
            return "zh-TW"
        if len(s) == 5 and s[2] == "-":
            base, region = s.split("-", 1)
            if base == "zh":
                return f"{base}-{region.upper()}"
            return base
        return s


def _canon_list(codes: Sequence[str]) -> List[str]:
    """
    Normalisiert und dedupliziert eine Liste von Sprachcodes.
    """
    out: List[str] = []
    seen = set()
    for c in codes or []:
        n = _canon_lang(c)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _filter_allowed(codes: Sequence[str], allowed: Sequence[str]) -> List[str]:
    """
    Filtert eine Liste von Codes gegen eine Allowed-Liste (beide kanonisch).
    """
    if not allowed:
        return list(codes)
    a = set(_canon_list(allowed))
    out: List[str] = []
    for c in codes:
        try:
            if not c:
                continue
            cc = _canon_lang(c) or ""
            base = cc.split("-")[0]
            if cc in a or base in a:
                out.append(cc)
        except Exception:
            continue
    return out


# ---------------------------
# HTTP-Helper
# ---------------------------


def _http_get_json(url: str, timeout: float) -> Tuple[Any, int, int, Optional[str]]:
    """
    Führt einen HTTP-GET auf url aus und versucht JSON zu parsen.

    Rückgabe:
      (payload, status, latency_ms, error_type)

    payload ist dict oder list, sonst {}.
    """
    t0 = time.perf_counter()
    try:
        # Primär: requests, wenn verfügbar
        if _HAS_REQUESTS and requests is not None:
            try:
                resp = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})  # type: ignore[arg-type]
                try:
                    data: Any = resp.json()
                except Exception:
                    try:
                        data = json.loads(resp.text or "{}")
                    except Exception:
                        data = {}
                if not isinstance(data, (dict, list)):
                    data = {}
                return data, int(resp.status_code), int((time.perf_counter() - t0) * 1000), None
            except Exception as exc:
                log.warning("health_http_requests_failed url=%s err=%s", url, type(exc).__name__)

        # Fallback: urllib, immer verfügbar
        try:
            req = Request(url, headers={"Accept": "application/json"})  # type: ignore[arg-type]
            with urlopen(req, timeout=timeout) as r:  # type: ignore[arg-type]  # nosec B310
                raw = r.read()
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    data = {}
                if not isinstance(data, (dict, list)):
                    data = {}
                status = int(getattr(r, "status", 200) or 200)
                return data, status, int((time.perf_counter() - t0) * 1000), None
        except URLError as exc:
            log.warning("health_http_urllib_failed url=%s err=%s", url, type(exc).__name__)
            return {}, 599, int((time.perf_counter() - t0) * 1000), type(exc).__name__
    except Exception as exc:
        # Nur unerwartete interne Fehler landen hier
        log.warning("health_http_unexpected_error url=%s err=%s", url, type(exc).__name__)
        return {}, 599, int((time.perf_counter() - t0) * 1000), type(exc).__name__

    # Sollte praktisch nur bei sehr exotischen Fällen erreicht werden.
    return {}, 599, int((time.perf_counter() - t0) * 1000), "unknown_error"


def _parse_langs(payload: Any) -> List[str]:
    """
    Extrahiert und normalisiert Sprachcodes aus einem /languages-Payload.

    Akzeptiert:
      - LibreTranslate: List[{"code": "..."}]
      - Proxy: {"languages":[...]} oder {"data":[...]}
      - Roh-List[str]

    Nutzt core.languages.from_engine_code, falls verfügbar.
    """
    out: List[str] = []
    try:
        if isinstance(payload, list):
            items: Any = payload
        elif isinstance(payload, dict):
            if isinstance(payload.get("languages"), list):
                items = payload.get("languages")
            elif isinstance(payload.get("data"), list):
                items = payload.get("data")
            else:
                items = payload.get("languages") or []
        else:
            items = []

        for it in items or []:
            if isinstance(it, dict):
                c = it.get("code") or it.get("lang") or it.get("id")
            else:
                c = it
            eng = str(c) if c is not None else ""
            if not eng:
                continue
            try:
                from core.languages import from_engine_code  # type: ignore

                can = from_engine_code(eng, engine="libretranslate")
            except Exception:
                can = eng
            n = _canon_lang(can)
            if n:
                out.append(n)
    except Exception:
        log.warning("health_parse_langs_failed", exc_info=True)
    # dedupliziert sortiert
    return sorted(set(out))


# ---------------------------
# Snapshot Upstream-Status
# ---------------------------


def _snapshot_from_state() -> Optional[Dict[str, Any]]:
    """
    Nutzt optional einen Upstream-Monitor (extensions['upstream_state']), falls vorhanden.

    Erwartete Keys im Snapshot (best effort):
      - last_status
      - available_langs
      - min_langs
      - ready
      - error
    """
    st = current_app.extensions.get("upstream_state")  # type: ignore
    if not st:
        return None
    try:
        snap = st.snapshot()  # type: ignore[attr-defined]
        status = snap.get("last_status")
        return {
            "status": int(status or 0) if status is not None else None,
            "available": list(snap.get("available_langs") or []),
            "min": list(snap.get("min_langs") or []),
            "ready": bool(snap.get("ready")),
            "error": snap.get("error"),
            "latency_ms": None,  # Monitor liefert diese Info typischerweise nicht
            "source": "monitor",
        }
    except Exception:
        log.warning("health_snapshot_from_state_failed", exc_info=True)
        return None


def _snapshot_direct() -> Dict[str, Any]:
    """
    Fragt den Upstream direkt über /languages ab und verwendet einen lokalen Cache.
    """
    interval = _cfg_float("READY_PROBE_INTERVAL_SEC", 30.0)
    timeout = _cfg_float("UPSTREAM_TIMEOUT_SEC", _cfg_float("REQUEST_TIMEOUT_SEC", 10.0))
    now = time.time()

    with _LOCK:
        ts = float(_CACHE.get("ts") or 0.0)
        cached_status = _CACHE.get("status")
        if now - ts < interval and cached_status is not None:
            return {
                "status": cached_status,
                "available": list(_CACHE.get("available") or []),
                "min": list(
                    current_app.config.get("READY_MIN_LANGS", ())
                    or current_app.config.get("ALLOWED_LANGS", ())
                ),
                "ready": None,  # wird in _build_ready_payload berechnet
                "error": _CACHE.get("error"),
                "latency_ms": _CACHE.get("latency_ms"),
                "source": "cache",
            }

    base = str(current_app.config.get("LT_BASE_URL", "http://libretranslate:5000")).rstrip("/")
    data, status, latency_ms, err = _http_get_json(f"{base}/languages", timeout=timeout)
    langs = _parse_langs(data)
    allowed = _canon_list(tuple(current_app.config.get("ALLOWED_LANGS", ())))
    langs = _filter_allowed(langs, allowed)

    with _LOCK:
        _CACHE["ts"] = now
        _CACHE["status"] = status
        _CACHE["available"] = langs
        _CACHE["latency_ms"] = latency_ms
        _CACHE["error"] = err

    return {
        "status": status,
        "available": langs,
        "min": list(
            current_app.config.get("READY_MIN_LANGS", ())
            or current_app.config.get("ALLOWED_LANGS", ())
        ),
        "ready": None,
        "error": err,
        "latency_ms": latency_ms,
        "source": "direct",
    }


def _build_ready_payload(*, live: bool = False) -> Tuple[Dict[str, Any], int]:
    """
    Baut das Payload für /_ready.

    live=True erzwingt einen direkten Upstream-Call (ignoriert Monitor-Snapshot und Cache nur eingeschränkt).
    """
    wait_for_up = bool(current_app.config.get("WAIT_FOR_UPSTREAM", False))
    allowed = _canon_list(tuple(current_app.config.get("ALLOWED_LANGS", ())))
    minimum = _canon_list(tuple(current_app.config.get("READY_MIN_LANGS", ())) or allowed)

    # Quelle für Upstream-Status bestimmen
    snap: Optional[Dict[str, Any]] = None
    source = "direct"
    if not live:
        snap = _snapshot_from_state()
        if snap is not None:
            source = "monitor"

    if snap is None:
        snap = _snapshot_direct()
        source = snap.get("source") or "direct"

    available = _canon_list(snap.get("available") or [])
    status_val = snap.get("status")
    status = int(status_val or 0) if status_val is not None else 0
    latency_ms = snap.get("latency_ms")

    reachable = 200 <= status < 300
    missing_required = [c for c in minimum if c not in available]
    ready_core = reachable and not missing_required
    ready = True if not wait_for_up else bool(ready_core)

    payload = {
        "ready": ready,
        "wait_for_upstream": wait_for_up,
        "live": bool(live),
        "upstream": {
            "source": source,
            "reachable": reachable,
            "status": status if status else None,
            "latency_ms": latency_ms,
            "langs_available": available,
            "missing_required": missing_required,
            "required_min": minimum,
            "error": snap.get("error"),
        },
        "service": {
            "name": current_app.config.get("SERVICE_NAME", "translate-svc"),
            "version": current_app.config.get("VERSION", "0.1.0"),
            "languages_allowed": allowed,
        },
    }
    http_status = 200 if ready else 503
    return payload, http_status


# ---------------------------
# Routes
# ---------------------------


@bp.get("/_health")
def health() -> Response:
    """
    Einfache Prozess-Health:
      - gibt 200 zurück, wenn der Flask-Prozess läuft.
      - prüft nicht den Upstream.
    """
    return jsonify(
        {
            "status": "ok",
            "service": current_app.config.get("SERVICE_NAME", "translate-svc"),
            "version": current_app.config.get("VERSION", "0.1.0"),
        }
    )


@bp.get("/_ready")
def ready() -> Response:
    """
    Readiness-Endpoint.

    - berücksichtigt Upstream-Erreichbarkeit und Mindestsprachen.
    - Query-Parameter:
        * live=1 → erzwingt direkten Upstream-Check, ignoriert Monitor-Snapshot soweit möglich.
    """
    live = _parse_bool_query("live", False)
    payload, http_status = _build_ready_payload(live=live)
    return jsonify(payload), http_status


@bp.get("/healthz")
def healthz() -> Response:
    """
    Alias für /_health (z. B. für Legacy-Healthchecks).
    """
    return health()
