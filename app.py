# services/language/app.py
from __future__ import annotations

"""
Flask-App-Fabrik für translate-svc.

Ziele
-----
- Zentrale Stelle zum Erzeugen der WSGI-App (create_app()).
- Lädt Settings aus config.Settings und überträgt sie nach app.config.
- Registriert alle Blueprints (health, translate, status).
- Bindet Request-Kontext (Request-ID, Client-Infos) und Logging.
- Initialisiert Cache und Rate-Limiter (InMemory/Redis), ohne Exceptions zu leaken.
"""

import logging
from typing import Any, Dict, Optional

from flask import Flask

# CORS ist optional – defensiver Import
try:  # pragma: no cover
    from flask_cors import CORS  # type: ignore
except Exception:  # pragma: no cover

    def CORS(*args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        return None


# Zentrale Settings / Logging
try:
    from config import Settings  # type: ignore
except Exception as _cfg_exc:  # pragma: no cover
    raise RuntimeError(f"cannot import Settings from config: {type(_cfg_exc).__name__}") from _cfg_exc

try:
    from core.logging import setup_logging, get_logger  # type: ignore
except Exception as _log_exc:  # pragma: no cover
    logging.getLogger(__name__).warning("core.logging_import_failed: %s", _log_exc)
    # Fallbacks
    def setup_logging(level: str = "INFO") -> None:  # type: ignore[override]
        logging.basicConfig(level=getattr(logging, (level or "INFO").upper(), logging.INFO))

    def get_logger(name: Optional[str] = None) -> logging.Logger:  # type: ignore[override]
        return logging.getLogger(name or __name__)

# Cache / Rate-Limit
try:
    from core.cache import build_cache_from_config  # type: ignore
except Exception as _cache_exc:  # pragma: no cover

    def build_cache_from_config(config: Dict[str, Any]):  # type: ignore[override]
        from core.cache import NoopCache  # type: ignore

        logging.getLogger(__name__).warning("cache_build_from_config_failed: %s", _cache_exc)
        return NoopCache()

try:
    from core.ratelimit import build_rate_limiter_from_config  # type: ignore
except Exception as _rl_exc:  # pragma: no cover

    def build_rate_limiter_from_config(config: Dict[str, Any]):  # type: ignore[override]
        from core.ratelimit import NoopLimiter  # type: ignore

        logging.getLogger(__name__).warning("ratelimit_build_from_config_failed: %s", _rl_exc)
        return NoopLimiter()

# Request-Kontext-Middleware
try:
    from middleware.request_context import attach_context, clear as clear_request_ctx, annotate_response  # type: ignore
except Exception:  # pragma: no cover

    def attach_context(request_id: Optional[str] = None) -> str:  # type: ignore[override]
        return "dev-no-request-id"

    def clear_request_ctx() -> None:  # type: ignore[override]
        return

    def annotate_response(resp):  # type: ignore[override]
        return resp

# Routes/Blueprints
try:
    from routes.health import bp as health_bp  # type: ignore
    from routes.translate import bp as translate_bp  # type: ignore
    from routes.status import bp as status_bp  # type: ignore
except Exception as _bp_exc:  # pragma: no cover
    raise RuntimeError(f"cannot import blueprints: {type(_bp_exc).__name__}") from _bp_exc


_LOG = get_logger(__name__)


def _configure_app_from_settings(app: Flask, settings: Settings) -> None:
    """
    Überträgt Settings nach app.config und setzt sinnvolle Defaults.
    """
    try:
        cfg = settings.as_dict()
    except Exception:  # pragma: no cover
        _LOG.exception("settings_as_dict_failed")
        cfg = {}

    try:
        app.config.update(cfg)
    except Exception:  # pragma: no cover
        _LOG.exception("app_config_update_failed")

    # Flask-spezifische JSON-Optionen (defensiv)
    try:
        app.config.setdefault("JSON_AS_ASCII", False)
    except Exception:
        pass

    # Maximalgröße für Requests (z. B. große Texte)
    try:
        mcl = int(getattr(settings, "MAX_CONTENT_LENGTH", 2 * 1024 * 1024))
        app.config["MAX_CONTENT_LENGTH"] = max(64 * 1024, mcl)
    except Exception:
        app.config.setdefault("MAX_CONTENT_LENGTH", 2 * 1024 * 1024)


def _setup_cors_if_configured(app: Flask, settings: Settings) -> None:
    """
    Aktiviert CORS, wenn CORS_ORIGINS gesetzt ist.
    """
    try:
        origins = getattr(settings, "CORS_ORIGINS", None)
    except Exception:
        origins = None

    if not origins:
        return

    try:
        if isinstance(origins, str):
            origin_list = [o.strip() for o in origins.replace(";", ",").split(",") if o.strip()]
        elif isinstance(origins, (list, tuple)):
            origin_list = [str(o).strip() for o in origins if str(o).strip()]
        else:
            origin_list = []

        if not origin_list:
            return

        CORS(app, resources={r"/*": {"origins": origin_list}})
        _LOG.info("cors_enabled", extra={"cors": {"origins": origin_list}})
    except Exception:
        _LOG.exception("cors_setup_failed")


def _init_extensions(app: Flask, settings: Settings) -> None:
    """
    Initialisiert Cache und Rate-Limiter und trägt sie in app.extensions ein.
    """
    try:
        cache = build_cache_from_config(app.config)  # type: ignore[arg-type]
        app.extensions["translate_cache"] = cache
    except Exception:
        _LOG.exception("cache_init_failed")

    try:
        limiter = build_rate_limiter_from_config(app.config)  # type: ignore[arg-type]
        app.extensions["rate_limiter"] = limiter
    except Exception:
        _LOG.exception("rate_limiter_init_failed")

    # Settings selbst als Extension verfügbar machen (z. B. für Status-Endpunkte)
    try:
        app.extensions["settings"] = settings
    except Exception:
        pass


def _register_blueprints(app: Flask) -> None:
    """
    Registriert alle Blueprints des Microservice.
    """
    try:
        app.register_blueprint(health_bp)
    except Exception:
        _LOG.exception("register_blueprint_health_failed")

    try:
        app.register_blueprint(translate_bp)
    except Exception:
        _LOG.exception("register_blueprint_translate_failed")

    try:
        app.register_blueprint(status_bp)
    except Exception:
        _LOG.exception("register_blueprint_status_failed")


def _register_request_hooks(app: Flask) -> None:
    """
    Bindet Request-Hooks für Kontext/Logging und Antwort-Annotation.
    """

    @app.before_request
    def _before_request() -> None:  # type: ignore[override]
        try:
            attach_context()
        except Exception:
            # Kontextfehler dürfen Requests nicht brechen
            _LOG.warning("attach_context_failed", exc_info=True)

    @app.after_request
    def _after_request(resp):  # type: ignore[override]
        try:
            annotate_response(resp)
        except Exception:
            # Response-Modifikation ist optional
            _LOG.warning("annotate_response_failed", exc_info=True)
        return resp

    @app.teardown_request
    def _teardown_request(exc: Optional[BaseException]) -> None:  # type: ignore[override]
        try:
            clear_request_ctx()
        except Exception:
            _LOG.warning("clear_request_ctx_failed", exc_info=True)


def create_app(extra_config: Optional[Dict[str, Any]] = None) -> Flask:
    """
    App-Fabrik für translate-svc.

    - Richtet Logging über core.logging.setup_logging ein.
    - Lädt Settings aus ENV.
    - Konfiguriert Flask-App und registriert Blueprints/Hooks/Extensions.

    Parameter
    ---------
    extra_config:
        Optionales Dict, das zusätzliche Config-Overrides enthält
        (z. B. für Tests).
    """
    # Logging global initialisieren (idempotent)
    try:
        settings_for_log = Settings()
        setup_logging(settings_for_log.LOG_LEVEL)
    except Exception:
        # Fallback-Log-Level
        setup_logging("INFO")
        _LOG.exception("setup_logging_with_settings_failed")

    # App + Settings initialisieren
    try:
        settings = Settings()
    except Exception as exc:  # pragma: no cover
        _LOG.exception("settings_init_failed")
        raise RuntimeError(f"settings_init_failed: {type(exc).__name__}") from exc

    app = Flask(__name__)
    _configure_app_from_settings(app, settings)

    if extra_config:
        try:
            app.config.update(extra_config)
        except Exception:
            _LOG.exception("extra_config_update_failed")

    _setup_cors_if_configured(app, settings)
    _init_extensions(app, settings)
    _register_blueprints(app)
    _register_request_hooks(app)

    _LOG.info(
        "app_created",
        extra={
            "app": {
                "service": app.config.get("SERVICE_NAME", "translate-svc"),
                "version": app.config.get("VERSION", app.config.get("APP_VERSION", "0.1.0")),
                "allowed_langs": app.config.get("ALLOWED_LANGS"),
            }
        },
    )

    return app
