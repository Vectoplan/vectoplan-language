# services/language/routes/status.py
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from flask import Blueprint, jsonify, current_app, request

bp = Blueprint("status", __name__)
log = logging.getLogger(__name__)


# ----------------------------- Hilfsfunktionen ----------------------------- #


def _canon_lang(code: str) -> str:
    """
    Kanonisiert Sprachcodes möglichst über core.languages, mit robustem Fallback.
    """
    try:
        from core.languages import canon_lang as _cl  # type: ignore

        return _cl(code)
    except Exception:
        try:
            c = (code or "").strip().replace("_", "-").lower()
        except Exception:  # pragma: no cover
            return ""
        if c in ("zh", "zh-cn", "zh-hans"):
            return "zh-CN"
        if c in ("zh-tw", "zh-hant", "zh-hk"):
            return "zh-TW"
        if len(c) == 5 and c[2] == "-":
            base, region = c.split("-", 1)
            if base == "zh":
                return f"{base}-{region.upper()}"
            return base
        return c


def _canon_list(items: Sequence[str]) -> List[str]:
    """
    Normalisiert und dedupliziert eine Liste von Sprachcodes.
    """
    seen = set()
    out: List[str] = []
    try:
        for x in items or []:
            cx = _canon_lang(x)
            if cx and cx not in seen:
                seen.add(cx)
                out.append(cx)
    except Exception:
        # Fehler im Status-Endpoint dürfen die Antwort nicht verhindern.
        log.warning("status_canon_list_failed", exc_info=True)
    return out


def _parse_langs_from_config(value: Any) -> List[str]:
    """
    Extrahiert eine Liste von Sprachcodes aus current_app.config-Werten.

    Unterstützt:
    - None → []
    - str  → Split an , und ;
    - Sequence → str() auf Elemente
    """
    try:
        if value is None:
            return []
        if isinstance(value, str):
            parts = [p.strip() for p in value.replace(";", ",").split(",") if p.strip()]
            return parts
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            out: List[str] = []
            for x in value:
                s = str(x).strip()
                if s:
                    out.append(s)
            return out
    except Exception:
        log.warning("status_parse_langs_from_config_failed", exc_info=True)
    return []


def _allowed() -> Tuple[str, ...]:
    """
    Liefert die aktuell konfigurierten Allowed-Sprachen (kanonisch, dedupliziert).
    """
    try:
        raw = current_app.config.get("ALLOWED_LANGS", ())
        items = _parse_langs_from_config(raw)
        if not items:
            # Fallback: es sollten in der Praxis immer Werte vorhanden sein.
            return tuple()
        return tuple(_canon_list(items))
    except Exception:
        log.warning("status_allowed_failed", exc_info=True)
        return tuple()


def _install_plan() -> List[str]:
    """
    Liest LT_INSTALL_LANGS aus der Config und normalisiert sie.
    """
    try:
        raw = current_app.config.get("LT_INSTALL_LANGS", "")
        items = _parse_langs_from_config(raw)
        return _canon_list(items)
    except Exception:
        log.warning("status_install_plan_failed", exc_info=True)
        return []


def _snapshot_upstream_from_monitor() -> Optional[Tuple[List[str], int, Optional[int], Optional[str]]]:
    """
    Nutzt optional den Upstream-Monitor (extensions['upstream_state']), falls vorhanden.

    Liefert:
      (available_langs, http_status, latency_ms, error_type)
    Wobei latency_ms hier immer None ist, da der Monitor diese Info nicht bereitstellt.
    """
    st = current_app.extensions.get("upstream_state")  # type: ignore
    if not st:
        return None
    try:
        snap = st.snapshot()  # type: ignore[attr-defined]
        langs_raw = snap.get("available_langs") or []
        langs = _canon_list([str(c) for c in langs_raw])
        status = snap.get("last_status")
        http_status = int(status) if status is not None else 0
        err = snap.get("error")
        return langs, http_status, None, err
    except Exception:
        log.warning("status_snapshot_upstream_failed", exc_info=True)
        return None


def _upstream_languages(timeout_sec: int) -> Tuple[List[str], int, Optional[int], Optional[str]]:
    """
    Liefert (available_langs, http_status, latency_ms, error_type) via direkten Upstream-Call.
    """
    base = str(current_app.config.get("LT_BASE_URL", "http://libretranslate:5000")).rstrip("/")
    started = time.perf_counter()
    try:
        # Präferiere internen HttpClient (Retries, Limits, JSON-sicher)
        try:
            from clients.http import HttpClient, HttpClientConfig  # type: ignore

            http = HttpClient(
                HttpClientConfig(base_url=base, timeout_sec=max(1, int(timeout_sec)))
            )
            data, status, _ = http.get_json("/languages", expected=(200,))
        except Exception:
            # Fallback auf urllib
            import json
            from urllib.request import Request, urlopen  # type: ignore
            from urllib.error import URLError, HTTPError  # type: ignore

            req = Request(f"{base}/languages", headers={"Accept": "application/json"})
            try:
                with urlopen(req, timeout=max(1, int(timeout_sec))) as r:  # nosec B310
                    raw = r.read().decode("utf-8", errors="replace")
                    status = int(getattr(r, "status", 200) or 200)
                    try:
                        data = json.loads(raw)
                    except Exception:
                        data = {}
            except HTTPError as e:
                return (
                    [],
                    int(getattr(e, "code", 502) or 502),
                    int((time.perf_counter() - started) * 1000),
                    type(e).__name__,
                )
            except URLError as e:
                return (
                    [],
                    599,
                    int((time.perf_counter() - started) * 1000),
                    type(e).__name__,
                )

        # Normalisieren
        items: List[Any] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("languages") or data.get("data") or []

        langs: List[str] = []
        for it in items:
            try:
                eng = ""
                if isinstance(it, dict):
                    eng = str(it.get("code") or it.get("lang") or it.get("id") or "").strip()
                else:
                    eng = str(it).strip()
                if not eng:
                    continue
                try:
                    from core.languages import from_engine_code  # type: ignore

                    can = from_engine_code(eng, engine="libretranslate")
                except Exception:
                    can = eng
                langs.append(_canon_lang(can))
            except Exception:
                continue

        langs = _canon_list(langs)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return langs, int(status), latency_ms, None
    except Exception as exc:
        return [], 599, int((time.perf_counter() - started) * 1000), type(exc).__name__


def _probe_fs() -> Tuple[List[str], Dict[str, Any]]:
    """
    Scannt das gemountete LT-Datenverzeichnis nach Argos-Paketen.
    """
    enabled = False
    base = "/ltdata"
    try:
        enabled = bool(current_app.config.get("FS_PROBE_ENABLED", False))
        base = str(current_app.config.get("LT_DATA_MOUNT", "/ltdata"))
    except Exception:
        pass

    info: Dict[str, Any] = {"enabled": enabled, "mount": base, "detected_count": 0, "staged_langs": []}
    if not enabled:
        return [], info

    candidates = [
        os.path.join(base, "share", "argos-translate", "packages"),
        os.path.join(base, "argos-translate", "packages"),
    ]

    found: List[str] = []
    try:
        for root in candidates:
            try:
                if not os.path.isdir(root):
                    continue
                for name in os.listdir(root):
                    n = str(name)
                    m = re.match(r"translate-([A-Za-z\-_.]+)_([A-Za-z\-_.]+)", n)
                    if not m:
                        continue
                    src, tgt = m.group(1), m.group(2)
                    # Auf Zielsprachen relativ zu en/de abbilden
                    if src.lower().startswith("en"):
                        found.append(_canon_lang(tgt))
                    elif tgt.lower().startswith("en"):
                        found.append(_canon_lang(src))
                    else:
                        # falls mal Paare ohne en vorhanden sind
                        found.append(_canon_lang(src))
                        found.append(_canon_lang(tgt))
            except Exception:
                continue
    except Exception:
        log.warning("status_probe_fs_failed", exc_info=True)

    staged = _canon_list(found)
    info["detected_count"] = len(staged)
    info["staged_langs"] = staged
    return staged, info


def _tail_installing(max_lines: int = 50) -> List[str]:
    """
    Liest letzte Install-Vorgänge aus /ltdata/install.log, falls vorhanden.
    """
    out: List[str] = []
    path = "/ltdata/install.log"
    try:
        base = str(current_app.config.get("LT_DATA_MOUNT", "/ltdata"))
        path = os.path.join(base, "install.log")
    except Exception:
        pass

    try:
        if not os.path.isfile(path):
            return out
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-max_lines:]
        for ln in lines:
            try:
                m = re.search(r"installing\s+([A-Za-z\-_]+)\s+<->\s+en", ln)
                if m:
                    out.append(_canon_lang(m.group(1)))
            except Exception:
                continue
    except Exception:
        # Kein harter Fehler – Status-Endpoint soll trotzdem liefern
        return []

    # Deduplizieren bei letzter Sichtung zuerst
    seen = set()
    res: List[str] = []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res


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


# --------------------------------- Route ---------------------------------- #


@bp.get("/languages/status")
def languages_status():
    """
    Übersicht zum Installations- und Verfügbarkeitsstatus:

      - allowed: konfigurierte Zielsprachen (Whitelist)
      - install_plan: Sprachplan aus LT_INSTALL_LANGS
      - available: Upstream-Sprachen (via Monitor oder Live-Call)
      - fs_probe: Pakete im LT_DATA_MOUNT-Volume
      - installing: laufende Installationen aus install.log
      - progress_pct, pending_count: Fortschritt relativ zum Plan
      - upstream: reachable/status/latency_ms/error
      - service: name/version/default_targets_source/wait_for_upstream

    Query-Parameter
    ----------------
      - details=1  → zusätzliche Felder (pending)
      - live=1     → Upstream-Status immer live von LibreTranslate holen,
                     statt optionalen Monitor-Snapshot zu nutzen.
    """
    details = _parse_bool_query("details", False)
    live = _parse_bool_query("live", False)

    # Konfiguration
    try:
        timeout = int(
            current_app.config.get(
                "UPSTREAM_TIMEOUT_SEC",
                current_app.config.get("REQUEST_TIMEOUT_SEC", 10),
            )
        )
    except Exception:
        timeout = 10

    # Quellen sammeln (robust, jede Teilquelle einzeln abgesichert)
    allowed = list(_allowed())
    plan = _install_plan()

    # Upstream: entweder Monitor-Snapshot oder live
    available: List[str]
    up_status: int
    up_latency: Optional[int]
    up_err: Optional[str]

    snap = None if live else _snapshot_upstream_from_monitor()
    if snap is not None:
        available, up_status, up_latency, up_err = snap
    else:
        available, up_status, up_latency, up_err = _upstream_languages(timeout)

    # Auf allowed filtern, falls gesetzt
    try:
        if allowed:
            allowed_set = set(allowed)
            available = [c for c in available if c in allowed_set]
    except Exception:
        pass

    fs_langs, fs_info = _probe_fs()
    installing = _tail_installing()

    # Fortschritt berechnen
    try:
        plan_set = set(plan)
        done_set = set([c for c in available if c not in {"en", "de"}]) | set(fs_langs)
        pending = sorted(list(plan_set - done_set))
        total = len(plan_set)
        progress = int(round(100.0 * (len(plan_set) - len(pending)) / total)) if total > 0 else 100
        pending_count = len(pending)
    except Exception:
        pending = []
        pending_count = len(plan or [])
        progress = 0

    # Payload
    payload: Dict[str, Any] = {
        "allowed": allowed,
        "install_plan": plan,
        "available": available,
        "installing": installing,
        "pending_count": pending_count,
        "progress_pct": progress,
        "service": {
            "name": current_app.config.get("SERVICE_NAME", "translate-svc"),
            "version": current_app.config.get("VERSION", "0.1.0"),
            "default_targets_source": str(current_app.config.get("DEFAULT_TARGETS_SOURCE", "available")),
            "wait_for_upstream": bool(current_app.config.get("WAIT_FOR_UPSTREAM", False)),
        },
        "upstream": {
            "base_url": str(current_app.config.get("LT_BASE_URL", "http://libretranslate:5000")),
            "last_check_ts": int(time.time() * 1000),
            "reachable": bool(200 <= int(up_status) < 300),
            "status": int(up_status) if up_status else None,
            "latency_ms": int(up_latency) if up_latency is not None else None,
            "error": up_err,
        },
        "fs_probe": fs_info,
    }

    if details:
        payload["pending"] = pending

    return jsonify(payload), 200
