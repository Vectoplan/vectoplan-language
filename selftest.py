# services/language/selftest.py
"""
Startup-Selftest für translate-svc.

Ziele
-----
- Nach Start des Dienstes End-to-End prüfen:
  - /_ready (Upstream/Sprachen) erreichbar
  - /translate liefert sinnvolle Antwort für Beispieltext
- Selftest darf niemals den Prozess crashen.
- Konfigurierbar über ENV, per Default aktiviert.

Typische Nutzung
----------------
- Aus einem Hook (z. B. gunicorn.when_ready oder post_worker_init) aufrufen:
    from language.selftest import start_background_selftest
    start_background_selftest()

- Alternativ synchron testen:
    from language.selftest import run_selftest_from_env
    ok = run_selftest_from_env()
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# Optionales HTTP-Backend
try:  # pragma: no cover
    import requests  # type: ignore

    _HAS_REQUESTS = True
except Exception:  # pragma: no cover
    _HAS_REQUESTS = False
    from urllib.request import Request, urlopen  # type: ignore
    from urllib.error import URLError  # type: ignore


# -----------------------------------------------------------------------------
# Konfiguration
# -----------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return default
        v = raw.strip().lower()
        return v in {"1", "true", "yes", "y", "on"}
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_str(name: str, default: str) -> str:
    try:
        val = os.getenv(name)
        return val if val else default
    except Exception:
        return default


@dataclass(frozen=True)
class SelfTestConfig:
    enabled: bool
    base_url: str
    ready_path: str
    translate_path: str
    max_wait_sec: int
    retry_interval_sec: float
    timeout_sec: int
    sample_text: str
    sample_source: str = "auto"

    def ready_url(self) -> str:
        base = self.base_url.rstrip("/")
        path = self.ready_path if self.ready_path.startswith("/") else f"/{self.ready_path}"
        return f"{base}{path}"

    def translate_url(self) -> str:
        base = self.base_url.rstrip("/")
        path = self.translate_path if self.translate_path.startswith("/") else f"/{self.translate_path}"
        return f"{base}{path}"


def load_config_from_env() -> SelfTestConfig:
    """
    Lädt Selftest-Konfiguration aus ENV.

    Relevante ENV-Variablen
    -----------------------
      SELFTEST_ENABLED           (default: 1)
      SELFTEST_BASE_URL          (default: http://127.0.0.1:{PORT|8000})
      SELFTEST_READY_PATH        (default: /_ready)
      SELFTEST_TRANSLATE_PATH    (default: /translate)
      SELFTEST_MAX_WAIT_SEC      (default: 60)
      SELFTEST_RETRY_INTERVAL_SEC(default: 2.0)
      SELFTEST_TIMEOUT_SEC       (default: 5)
      SELFTEST_SAMPLE_TEXT       (default: deutscher Beispieltext)
    """
    enabled = _env_bool("SELFTEST_ENABLED", True)

    # Basis-URL: nutzt PORT, falls gesetzt
    port = _env_int("PORT", 8000)
    default_base = f"http://127.0.0.1:{port}"
    base_url = _env_str("SELFTEST_BASE_URL", default_base)

    ready_path = _env_str("SELFTEST_READY_PATH", "/_ready")
    translate_path = _env_str("SELFTEST_TRANSLATE_PATH", "/translate")

    max_wait_sec = max(1, _env_int("SELFTEST_MAX_WAIT_SEC", 60))
    retry_interval_sec = max(0.5, _env_float("SELFTEST_RETRY_INTERVAL_SEC", 2.0))
    timeout_sec = max(1, _env_int("SELFTEST_TIMEOUT_SEC", 5))

    sample_default = (
        "Hallo zusammen, mir geht es heute gut und wie geht es dir?"
    )
    sample_text = _env_str("SELFTEST_SAMPLE_TEXT", sample_default)

    return SelfTestConfig(
        enabled=enabled,
        base_url=base_url,
        ready_path=ready_path,
        translate_path=translate_path,
        max_wait_sec=max_wait_sec,
        retry_interval_sec=retry_interval_sec,
        timeout_sec=timeout_sec,
        sample_text=sample_text,
        sample_source="auto",
    )


# -----------------------------------------------------------------------------
# HTTP Helpers
# -----------------------------------------------------------------------------


def _http_get_json(url: str, timeout_sec: int) -> Tuple[int, Any, Optional[str]]:
    """
    Führt GET gegen url aus und versucht JSON zu parsen.

    Rückgabe:
      (status, payload, error_type)
    payload ist dict/list oder {}.
    """
    t0 = time.perf_counter()
    try:
        if _HAS_REQUESTS:
            try:
                resp = requests.get(url, timeout=timeout_sec, headers={"Accept": "application/json"})  # type: ignore
                try:
                    data: Any = resp.json()
                except Exception:
                    try:
                        data = json.loads(resp.text or "{}")
                    except Exception:
                        data = {}
                if not isinstance(data, (dict, list)):
                    data = {}
                return int(resp.status_code), data, None
            except Exception as exc:
                log.warning(
                    "selftest_http_get_requests_failed url=%s err=%s",
                    url,
                    type(exc).__name__,
                )
        # urllib Fallback
        try:
            from urllib.request import Request, urlopen  # type: ignore
            from urllib.error import URLError, HTTPError  # type: ignore

            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout_sec) as r:  # nosec B310
                raw = r.read()
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    data = {}
                if not isinstance(data, (dict, list)):
                    data = {}
                status = int(getattr(r, "status", 200) or 200)
                return status, data, None
        except HTTPError as exc:  # type: ignore[name-defined]
            return int(getattr(exc, "code", 502) or 502), {}, type(exc).__name__
        except URLError as exc:  # type: ignore[name-defined]
            return 599, {}, type(exc).__name__
    except Exception as exc:
        log.warning(
            "selftest_http_get_unexpected_error url=%s err=%s dur_ms=%s",
            url,
            type(exc).__name__,
            int((time.perf_counter() - t0) * 1000),
        )
        return 599, {}, type(exc).__name__
    # Sollte praktisch nicht erreicht werden
    return 599, {}, "unknown_error"


def _http_post_json(url: str, payload: Dict[str, Any], timeout_sec: int) -> Tuple[int, Any, Optional[str]]:
    """
    Führt POST mit JSON-Body aus.

    Rückgabe:
      (status, payload, error_type)
    """
    t0 = time.perf_counter()
    body = json.dumps(payload, ensure_ascii=False)
    try:
        if _HAS_REQUESTS:
            try:
                resp = requests.post(  # type: ignore
                    url,
                    data=body.encode("utf-8"),
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    timeout=timeout_sec,
                )
                try:
                    data: Any = resp.json()
                except Exception:
                    try:
                        data = json.loads(resp.text or "{}")
                    except Exception:
                        data = {}
                if not isinstance(data, (dict, list)):
                    data = {}
                return int(resp.status_code), data, None
            except Exception as exc:
                log.warning(
                    "selftest_http_post_requests_failed url=%s err=%s",
                    url,
                    type(exc).__name__,
                )
        # urllib Fallback
        try:
            from urllib.request import Request, urlopen  # type: ignore
            from urllib.error import URLError, HTTPError  # type: ignore

            req = Request(
                url,
                data=body.encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with urlopen(req, timeout=timeout_sec) as r:  # nosec B310
                raw = r.read()
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    data = {}
                if not isinstance(data, (dict, list)):
                    data = {}
                status = int(getattr(r, "status", 200) or 200)
                return status, data, None
        except HTTPError as exc:  # type: ignore[name-defined]
            return int(getattr(exc, "code", 502) or 502), {}, type(exc).__name__
        except URLError as exc:  # type: ignore[name-defined]
            return 599, {}, type(exc).__name__
    except Exception as exc:
        log.warning(
            "selftest_http_post_unexpected_error url=%s err=%s dur_ms=%s",
            url,
            type(exc).__name__,
            int((time.perf_counter() - t0) * 1000),
        )
        return 599, {}, type(exc).__name__
    return 599, {}, "unknown_error"


# -----------------------------------------------------------------------------
# Kernlogik Self-Test
# -----------------------------------------------------------------------------


def _is_ready_payload_ok(payload: Any) -> bool:
    """
    Prüft, ob das Ready-Payload sinnvoll aussieht.
    """
    if not isinstance(payload, dict):
        return False
    ready = payload.get("ready")
    if ready is True:
        return True
    return False


def _is_translate_payload_ok(payload: Any) -> bool:
    """
    Prüft, ob die Antwort von /translate sinnvoll ist:
      - payload ist dict
      - enthält "translations" als Mapping
      - mindestens eine Übersetzung vorhanden
    """
    if not isinstance(payload, dict):
        return False
    tr = payload.get("translations")
    if not isinstance(tr, dict):
        return False
    if not tr:
        return False
    # Minimalprüfung: Strings als Values
    for k, v in tr.items():
        if not isinstance(k, str):
            return False
        if not isinstance(v, str):
            return False
    return True


def run_selftest(config: SelfTestConfig) -> bool:
    """
    Führt den Selftest synchron mit der gegebenen Konfiguration aus.

    Ablauf:
      1. Optional: Abbruch, wenn disabled.
      2. Warten auf /_ready (ready=true) bis max_wait_sec.
      3. POST /translate mit Beispieltext.
      4. Ergebnis loggen; gibt True/False zurück.

    Fehler schlagen sich nur im Log nieder, der Aufrufer bekommt ein bool.
    """
    if not config.enabled:
        log.info(
            "selftest_disabled",
            extra={"selftest": {"base_url": config.base_url, "reason": "SELFTEST_ENABLED=0"}},
        )
        return False

    ready_url = config.ready_url()
    translate_url = config.translate_url()

    log.info(
        "selftest_start",
        extra={
            "selftest": {
                "base_url": config.base_url,
                "ready_url": ready_url,
                "translate_url": translate_url,
                "max_wait_sec": config.max_wait_sec,
                "retry_interval_sec": config.retry_interval_sec,
                "timeout_sec": config.timeout_sec,
            }
        },
    )

    # 1) Auf Ready warten
    deadline = time.time() + config.max_wait_sec
    ready_ok = False
    last_status: Optional[int] = None
    last_error: Optional[str] = None

    while time.time() < deadline:
        status, payload, err = _http_get_json(ready_url, config.timeout_sec)
        last_status = status
        last_error = err
        if status == 200 and _is_ready_payload_ok(payload):
            ready_ok = True
            break
        time.sleep(config.retry_interval_sec)

    if not ready_ok:
        log.warning(
            "selftest_ready_timeout",
            extra={
                "selftest": {
                    "ready_url": ready_url,
                    "last_status": last_status,
                    "last_error": last_error,
                }
            },
        )
        return False

    log.info("selftest_ready_ok", extra={"selftest": {"ready_url": ready_url, "status": last_status}})

    # 2) Translate testen
    body: Dict[str, Any] = {
        "text": config.sample_text,
        "source": config.sample_source,
        # targets wird bewusst nicht gesetzt → Service nutzt Default-Zielsprachen
    }

    status_tr, payload_tr, err_tr = _http_post_json(translate_url, body, config.timeout_sec)

    if status_tr == 200 and _is_translate_payload_ok(payload_tr):
        try:
            src = payload_tr.get("source")
            tr = payload_tr.get("translations") or {}
            langs = list(tr.keys())
        except Exception:
            src = None
            langs = []
        log.info(
            "selftest_translate_ok",
            extra={
                "selftest": {
                    "translate_url": translate_url,
                    "status": status_tr,
                    "source": src,
                    "languages": langs,
                    "languages_count": len(langs),
                }
            },
        )
        return True

    log.warning(
        "selftest_translate_failed",
        extra={
            "selftest": {
                "translate_url": translate_url,
                "status": status_tr,
                "error": err_tr,
                "payload_type": type(payload_tr).__name__,
            }
        },
    )
    return False


# -----------------------------------------------------------------------------
# Public Helpers
# -----------------------------------------------------------------------------


def run_selftest_from_env() -> bool:
    """
    Lädt Konfiguration aus ENV und führt einen synchronen Selftest aus.

    Rückgabe:
      True  -> Selftest erfolgreich
      False -> Selftest deaktiviert oder fehlgeschlagen
    """
    try:
        cfg = load_config_from_env()
        return run_selftest(cfg)
    except Exception:
        log.warning("selftest_from_env_unexpected_error", exc_info=True)
        return False


_started_lock = threading.Lock()
_started_flag = False


def start_background_selftest(config: Optional[SelfTestConfig] = None) -> None:
    """
    Startet den Selftest in einem Hintergrund-Thread.

    - Stellt sicher, dass pro Prozess nur ein Selftest-Thread gestartet wird.
    - Thread ist als Daemon markiert.
    """
    global _started_flag
    with _started_lock:
        if _started_flag:
            return
        _started_flag = True

    if config is None:
        try:
            config = load_config_from_env()
        except Exception:
            log.warning("selftest_load_config_failed", exc_info=True)
            # Ohne Konfiguration macht ein Selftest keinen Sinn
            return

    def _worker() -> None:
        try:
            run_selftest(config)  # Ergebnis nur loggen
        except Exception:
            log.warning("selftest_background_unexpected_error", exc_info=True)

    t = threading.Thread(target=_worker, name="selftest-thread", daemon=True)
    try:
        t.start()
    except Exception:
        log.warning("selftest_thread_start_failed", exc_info=True)


# -----------------------------------------------------------------------------
# Optionaler CLI-Entry (z. B. python -m language.selftest)
# -----------------------------------------------------------------------------


def main() -> int:
    """
    CLI-Entry-Point:

      python -m language.selftest

    nutzt SELFTEST_* ENV-Variablen und gibt Exit-Code 0/1 zurück.
    """
    ok = run_selftest_from_env()
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
