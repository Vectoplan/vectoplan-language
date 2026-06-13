# services/language/wsgi.py
"""
WSGI-Entry für translate-svc.

Ziele
------
- Stellt `app` (und Alias `application`) für Gunicorn/uWSGI bereit.
- Nutzt die echte App-Fabrik `create_app()` aus app.py.
- Robuster Fallback, falls App-Initialisierung fehlschlägt (ImportError etc.).
- Optionaler Direktstart per `python wsgi.py` für lokale Entwicklung.
- Optionaler Startup-Selftest im Hintergrund (selftest.py), ohne den Prozess zu crashen.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

# stdout sofort flushen (bessere Log-Experience in Docker)
try:
    import sys as _sys

    _sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
except Exception:
    pass

_LOG = logging.getLogger(__name__)


def _fallback_app(err_type: str, err_msg: str):
    """
    Minimaler Fallback, der den Prozess nicht crasht.
    Liefert /, /_health und /_ready mit Fehlerstatus.

    Wird nur verwendet, wenn:
      - create_app() nicht importiert werden kann oder
      - create_app() beim Ausführen eine Exception wirft.
    """
    try:
        from flask import Flask, jsonify
    except Exception as exc:  # pragma: no cover
        # Harte Umgebung ohne Flask: Anwendungsstart scheitert
        raise RuntimeError(f"fatal: flask_not_available ({err_type}: {err_msg})") from exc

    app = Flask(__name__)

    @app.get("/")
    def _root():
        return (
            jsonify(
                {
                    "service": "translate-svc",
                    "version": os.getenv("APP_VERSION", "0.1.0"),
                    "ready": False,
                    "error": {"type": err_type, "message": err_msg},
                }
            ),
            500,
        )

    @app.get("/_health")
    def _health():
        return jsonify({"status": "error", "error": {"type": err_type}}), 500

    @app.get("/_ready")
    def _ready():
        return jsonify({"ready": False, "error": {"type": err_type}}), 503

    logging.getLogger(__name__).error(
        "wsgi_fallback_app_started type=%s msg=%s", err_type, err_msg
    )
    return app


def _start_selftest_if_available() -> None:
    """
    Startet den Startup-Selftest im Hintergrund, falls selftest.py vorhanden ist.

    - Nutzt SELFTEST_* ENV-Variablen für die Konfiguration.
    - Darf niemals Exceptions nach außen werfen.
    """
    try:
        from selftest import start_background_selftest, load_config_from_env  # type: ignore

        cfg = load_config_from_env()
        if not getattr(cfg, "enabled", True):
            _LOG.info("selftest_not_started_disabled", extra={"selftest": {"enabled": False}})
            return
        start_background_selftest(cfg)
        _LOG.info(
            "selftest_background_started",
            extra={
                "selftest": {
                    "base_url": cfg.base_url,
                    "max_wait_sec": cfg.max_wait_sec,
                    "retry_interval_sec": cfg.retry_interval_sec,
                    "timeout_sec": cfg.timeout_sec,
                }
            },
        )
    except Exception:
        # Selftest ist optional, Fehler nur loggen
        _LOG.warning("selftest_start_failed", exc_info=True)


def _create_app():
    """
    Versucht, die echte App zu erstellen. Fällt robust auf Fallback zurück.
    """
    try:
        from app import create_app  # aus services/language/app.py
    except Exception as exc:
        et, em = type(exc).__name__, str(exc)
        return _fallback_app(et, em)

    try:
        app = create_app()
        logging.getLogger(__name__).info(
            "wsgi_real_app_started",
            extra={
                "app": {
                    "service": getattr(app.config, "get", lambda *_: "translate-svc")(
                        "SERVICE_NAME", "translate-svc"
                    ),
                    "version": app.config.get("VERSION", app.config.get("APP_VERSION", "0.1.0")),
                }
            },
        )
        # Selftest optional im Hintergrund starten
        _start_selftest_if_available()
        return app
    except Exception as exc:
        et, em = type(exc).__name__, str(exc)
        logging.getLogger(__name__).exception("create_app_failed")
        return _fallback_app(et, em)


# Von WSGI-Servern erwartetes Objekt
app = _create_app()
application = app  # Alias gängig für uWSGI / andere WSGI-Server


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


if __name__ == "__main__":
    # Lokaler Start ohne Gunicorn, z. B.: python services/language/wsgi.py
    port = _env_int("PORT", 8000)
    host = os.getenv("HOST", "0.0.0.0")
    debug = str(os.getenv("FLASK_DEBUG", "0")).lower() in ("1", "true", "yes", "on")
    try:
        app.run(host=host, port=port, debug=debug)
    except Exception as exc:
        print(f"fatal: cannot start dev server: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
