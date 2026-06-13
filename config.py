# services/language/config.py
"""
Zentrale Konfiguration für translate-svc.

Wichtige ENV-Variablen (Auszug)
--------------------------------
  PORT=8000
  LOG_LEVEL=INFO
  PROXY_COUNT=1
  HTTP_TIMEOUT_SEC=30
  MAX_CONTENT_LENGTH=2097152

  # LibreTranslate Upstream
  LT_BASE_URL=http://libretranslate:5000
  LT_API_KEY=

  # Sprachen (Default-Zielsprachen für Fallback)
  # Wenn ALLOWED_LANGS nicht gesetzt ist, fällt der Service auf diese 5 zurück:
  #   en, de, es, fr, zh-CN
  ALLOWED_LANGS=en,de,es,fr,zh-CN
  READY_MIN_LANGS=en,de                  # Minimale Menge für Readiness
  DEFAULT_TARGETS_SOURCE=available       # 'available' | 'allowed' | 'minimum'

  # Rate-Limit
  RATE_LIMIT_ENABLED=0
  RATE_BUCKET=60
  RATE_REFILL=1

  # Cache / Redis
  CACHE_ENABLED=0
  CACHE_TTL_SEC=3600
  CACHE_MAX_ITEMS=10000
  CACHE_VERSION=1                        # Versionsnummer für Übersetzungs-Cache
  REDIS_URL=redis://redis:6379/0

  # CORS / Limits
  CORS_ORIGINS=
  MAX_CHARS=10000
  MAX_TARGETS=5

  # Readiness/Upstream-Probe
  WAIT_FOR_UPSTREAM=0                    # 1 = /_ready erst true wenn READY_MIN_LANGS verfügbar
  READY_PROBE_INTERVAL_SEC=30
  READY_START_DELAY_SEC=0
  UPSTREAM_TIMEOUT_SEC=10                # Fallback auf HTTP_TIMEOUT_SEC

  # Installationsstatus (für /languages/status)
  # Wenn LT_INSTALL_LANGS nicht gesetzt ist, wird auf diese 4 Nicht-Pivot-Sprachen
  # zurückgefallen (Pivot ist 'en'):
  #   de, es, fr, zh-CN
  LT_INSTALL_LANGS=de,es,fr,zh-CN
  INSTALL_STATUS_SOURCE=plan             # 'plan' | 'plan+fs'
  FS_PROBE_ENABLED=0                     # 1 = Dateisystem sondieren
  LT_DATA_MOUNT=/ltdata                  # read-only Mount mit Argos/Share
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional, Sequence, Tuple, Dict, Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sprachlisten: Single Source of Truth für diesen Service
# ---------------------------------------------------------------------------

# Default-Fallback für translate-svc, falls keine ENV gesetzt ist
DEFAULT_ALLOWED_LANGS: Tuple[str, ...] = ("en", "de", "es", "fr", "zh-CN")
DEFAULT_LT_INSTALL_LANGS: Tuple[str, ...] = ("de", "es", "fr", "zh-CN")

try:
    # Bevorzugt zentrale Sprachlogik aus core.languages verwenden
    from core.languages import (
        KNOWN_LANGS as _CORE_KNOWN_LANGS,
        canon_lang as _canon_lang,
        canon_targets as _canon_targets,
    )
except Exception:  # pragma: no cover - Fallback für sehr reduzierte Umgebungen
    _CORE_KNOWN_LANGS = DEFAULT_ALLOWED_LANGS + ("zh-TW",)

    def _canon_lang(code: str) -> str:
        """Minimaler Fallback für Sprach-Normalisierung."""
        c = (code or "").strip().replace("_", "-")
        if not c:
            return c
        low = c.lower()
        if len(low) == 5 and low[2] == "-":
            base, region = low.split("-", 1)
            if base == "zh":
                return f"{base}-{region.upper()}"
            return base
        if low == "zh":
            return "zh-CN"
        return low

    def _canon_targets(items: Iterable[str]) -> Tuple[str, ...]:
        seen = set()
        out: List[str] = []
        for x in items or []:
            cx = _canon_lang(x)
            if cx and cx not in seen:
                seen.add(cx)
                out.append(cx)
        return tuple(out)

# Für andere Module weiterhin als KNOWN_LANGS exportieren
KNOWN_LANGS: Tuple[str, ...] = tuple(_CORE_KNOWN_LANGS)


# ---------------------------------------------------------------------------
# ENV-Reader (robust)
# ---------------------------------------------------------------------------


def _env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    """
    Liest eine String-Variable aus der Umgebung.

    - Gibt None oder `default` zurück, wenn die Variable nicht gesetzt oder nur aus Whitespace besteht.
    - Trimmt führende/anhängende Leerzeichen, um Probleme mit z. B. REDIS_URL=" redis://..." zu vermeiden.
    """
    try:
        raw = os.getenv(name)
        if raw is None:
            return default
        val = raw.strip()
        if not val:
            return default
        return val
    except Exception:  # pragma: no cover
        log.warning("env_str_failed name=%s", name, exc_info=True)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:  # pragma: no cover
        log.warning("env_int_failed name=%s", name, exc_info=True)
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:  # pragma: no cover
        log.warning("env_float_failed name=%s", name, exc_info=True)
        return default


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return default
        v = v.strip().lower()
        return v in ("1", "true", "t", "yes", "y", "on")
    except Exception:  # pragma: no cover
        log.warning("env_bool_failed name=%s", name, exc_info=True)
        return default


def _env_list(name: str, default: Sequence[str]) -> List[str]:
    """
    Liest eine Liste von Strings aus ENV.

    Unterstützt:
    - Komma- oder Semikolon-getrennte Strings
    - Fällt auf `default` zurück, falls Variable fehlt/leer ist.
    """
    try:
        raw = os.getenv(name)
        if not raw:
            return list(default)
        return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    except Exception:  # pragma: no cover
        log.warning("env_list_failed name=%s", name, exc_info=True)
        return list(default)


# ---------------------------------------------------------------------------
# Sprach-Normalisierung (auf Basis core.languages)
# ---------------------------------------------------------------------------


def _normalize_langs(items: Iterable[str], *, drop_unknown: bool = True) -> Tuple[str, ...]:
    """
    Normalisiert eine beliebige Sequenz von Sprachcodes.

    - nutzt _canon_targets() → dedupliziert & normalisiert.
    - optional werden unbekannte Codes (nicht in KNOWN_LANGS) verworfen.
    """
    try:
        norm = _canon_targets(items)
        if not drop_unknown:
            return norm
        allowed = set(KNOWN_LANGS)
        out = []
        for c in norm:
            if c in allowed:
                out.append(c)
            else:
                log.warning("unknown_language_ignored_in_config: %s", c)
        return tuple(out)
    except Exception:  # pragma: no cover
        log.warning("normalize_langs_failed", exc_info=True)
        return tuple()


def _normalize_allowed_langs(items: Iterable[str]) -> Tuple[str, ...]:
    """
    Normalisiert ALLOWED_LANGS/READY_MIN_LANGS o.ä.

    Fallback: DEFAULT_ALLOWED_LANGS (5 Sprachen), falls nach Normalisierung nichts übrig bleibt.
    """
    try:
        out = _normalize_langs(items, drop_unknown=True)
        return out or DEFAULT_ALLOWED_LANGS
    except Exception:  # pragma: no cover
        log.warning("normalize_allowed_langs_failed_fallback_default", exc_info=True)
        return DEFAULT_ALLOWED_LANGS


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings:
    """
    Wrapper um ENV-Konfiguration mit robusten Defaults.

    Hinweis: Alle Felder sind bewusst einfach gehalten und enthalten nur bereits
    validierte/normalisierte Werte. Fehler im ENV sollen niemals den Start des
    Prozesses verhindern – es wird stattdessen auf sinnvolle Defaults
    zurückgefallen.
    """

    # Basis
    PORT: int
    LOG_LEVEL: str
    PROXY_COUNT: int
    REQUEST_TIMEOUT_SEC: int
    MAX_CONTENT_LENGTH: int

    # Upstream
    LT_BASE_URL: str
    LT_API_KEY: Optional[str]

    # Sprachen
    ALLOWED_LANGS: Tuple[str, ...]
    READY_MIN_LANGS: Tuple[str, ...]
    DEFAULT_TARGETS_SOURCE: str  # "available" | "allowed" | "minimum"

    # Rate-Limit
    RATE_LIMIT_ENABLED: bool
    RATE_BUCKET: int
    RATE_REFILL: int

    # Cache
    CACHE_ENABLED: bool
    CACHE_TTL_SEC: int
    CACHE_MAX_ITEMS: int
    CACHE_VERSION: int
    REDIS_URL: Optional[str]

    # CORS/JSON
    CORS_ORIGINS: Optional[str]
    JSON_SORT_KEYS: bool
    JSONIFY_PRETTYPRINT_REGULAR: bool

    # Payload/Targets
    MAX_CHARS: int
    MAX_TARGETS: int

    # Readiness/Probe
    WAIT_FOR_UPSTREAM: bool
    READY_PROBE_INTERVAL_SEC: float
    READY_START_DELAY_SEC: float
    UPSTREAM_TIMEOUT_SEC: int

    # Installationsstatus (/languages/status)
    LT_INSTALL_LANGS: Tuple[str, ...]
    INSTALL_STATUS_SOURCE: str  # "plan" | "plan+fs"
    FS_PROBE_ENABLED: bool
    LT_DATA_MOUNT: Optional[str]

    def __init__(self) -> None:
        try:
            # Basis
            self.PORT = max(1, min(65535, _env_int("PORT", 8000)))
            self.LOG_LEVEL = (_env_str("LOG_LEVEL", "INFO") or "INFO").upper()
            self.PROXY_COUNT = max(0, _env_int("PROXY_COUNT", 1))
            self.REQUEST_TIMEOUT_SEC = max(1, _env_int("HTTP_TIMEOUT_SEC", 30))
            self.MAX_CONTENT_LENGTH = max(64 * 1024, _env_int("MAX_CONTENT_LENGTH", 2 * 1024 * 1024))

            # Upstream
            self.LT_BASE_URL = _env_str("LT_BASE_URL", "http://libretranslate:5000") or "http://libretranslate:5000"
            self.LT_API_KEY = _env_str("LT_API_KEY", None)
            if not self.LT_BASE_URL.startswith(("http://", "https://")):
                logging.getLogger(__name__).warning("lt_base_url_invalid, fallback_to_http")
                self.LT_BASE_URL = "http://libretranslate:5000"

            # Sprachen (ENV → Fallback 5 Sprachen)
            raw_allowed = _env_list("ALLOWED_LANGS", DEFAULT_ALLOWED_LANGS)
            self.ALLOWED_LANGS = _normalize_allowed_langs(raw_allowed)

            raw_ready_min = _env_list("READY_MIN_LANGS", ("en", "de"))
            self.READY_MIN_LANGS = _normalize_allowed_langs(raw_ready_min)

            dts = (_env_str("DEFAULT_TARGETS_SOURCE", "available") or "available").lower()
            self.DEFAULT_TARGETS_SOURCE = dts if dts in ("available", "allowed", "minimum") else "available"

            # Rate-Limit
            self.RATE_LIMIT_ENABLED = _env_bool("RATE_LIMIT_ENABLED", False)
            self.RATE_BUCKET = max(1, _env_int("RATE_BUCKET", 60))
            self.RATE_REFILL = max(1, _env_int("RATE_REFILL", 1))

            # Cache
            self.CACHE_ENABLED = _env_bool("CACHE_ENABLED", False)
            self.CACHE_TTL_SEC = max(1, _env_int("CACHE_TTL_SEC", 3600))
            self.CACHE_MAX_ITEMS = max(100, _env_int("CACHE_MAX_ITEMS", 10000))
            self.CACHE_VERSION = max(1, _env_int("CACHE_VERSION", 1))
            self.REDIS_URL = _env_str("REDIS_URL", None)

            # CORS/JSON
            self.CORS_ORIGINS = _env_str("CORS_ORIGINS", None)
            self.JSON_SORT_KEYS = False
            self.JSONIFY_PRETTYPRINT_REGULAR = False

            # Payload/Targets
            self.MAX_CHARS = max(256, _env_int("MAX_CHARS", 10_000))
            self.MAX_TARGETS = max(1, _env_int("MAX_TARGETS", 5))

            # Readiness/Probe
            self.WAIT_FOR_UPSTREAM = _env_bool("WAIT_FOR_UPSTREAM", False)
            self.READY_PROBE_INTERVAL_SEC = max(1.0, _env_float("READY_PROBE_INTERVAL_SEC", 30.0))
            self.READY_START_DELAY_SEC = max(0.0, _env_float("READY_START_DELAY_SEC", 0.0))
            # Wenn UPSTREAM_TIMEOUT_SEC fehlt → HTTP_TIMEOUT_SEC
            self.UPSTREAM_TIMEOUT_SEC = max(1, _env_int("UPSTREAM_TIMEOUT_SEC", self.REQUEST_TIMEOUT_SEC))

            # Installationsstatus (ENV → Fallback 4 Sprachen)
            raw_install = _env_list("LT_INSTALL_LANGS", DEFAULT_LT_INSTALL_LANGS)
            self.LT_INSTALL_LANGS = _normalize_langs(raw_install, drop_unknown=True)

            iss = (_env_str("INSTALL_STATUS_SOURCE", "plan") or "plan").lower()
            self.INSTALL_STATUS_SOURCE = iss if iss in ("plan", "plan+fs") else "plan"

            self.FS_PROBE_ENABLED = _env_bool("FS_PROBE_ENABLED", False)
            self.LT_DATA_MOUNT = _env_str("LT_DATA_MOUNT", None)

        except Exception:
            # Harte Fallbacks – Service soll trotzdem startbar bleiben.
            logging.getLogger(__name__).exception("settings_init_failed_fallback_defaults")

            # Basis
            self.PORT = 8000
            self.LOG_LEVEL = "INFO"
            self.PROXY_COUNT = 1
            self.REQUEST_TIMEOUT_SEC = 30
            self.MAX_CONTENT_LENGTH = 2 * 1024 * 1024

            # Upstream
            self.LT_BASE_URL = "http://libretranslate:5000"
            self.LT_API_KEY = None

            # Sprachen (Fallback: 5 Sprachen)
            self.ALLOWED_LANGS = DEFAULT_ALLOWED_LANGS
            self.READY_MIN_LANGS = ("en", "de")
            self.DEFAULT_TARGETS_SOURCE = "available"

            # Rate-Limit
            self.RATE_LIMIT_ENABLED = False
            self.RATE_BUCKET = 60
            self.RATE_REFILL = 1

            # Cache
            self.CACHE_ENABLED = False
            self.CACHE_TTL_SEC = 3600
            self.CACHE_MAX_ITEMS = 10000
            self.CACHE_VERSION = 1
            self.REDIS_URL = None

            # CORS/JSON
            self.CORS_ORIGINS = None
            self.JSON_SORT_KEYS = False
            self.JSONIFY_PRETTYPRINT_REGULAR = False

            # Payload/Targets
            self.MAX_CHARS = 10_000
            self.MAX_TARGETS = 5

            # Readiness/Probe
            self.WAIT_FOR_UPSTREAM = False
            self.READY_PROBE_INTERVAL_SEC = 30.0
            self.READY_START_DELAY_SEC = 0.0
            self.UPSTREAM_TIMEOUT_SEC = 10

            # Installationsstatus (Fallback-Plan)
            self.LT_INSTALL_LANGS = DEFAULT_LT_INSTALL_LANGS
            self.INSTALL_STATUS_SOURCE = "plan"
            self.FS_PROBE_ENABLED = False
            self.LT_DATA_MOUNT = None

    def as_dict(self) -> Dict[str, Any]:
        """
        Liefert eine menschenlesbare Ansicht für Debug-/Statusendpoints.
        Sensible Werte (z. B. LT_API_KEY, REDIS_URL) werden nur als Bool angezeigt.
        """
        return {
            "PORT": self.PORT,
            "LOG_LEVEL": self.LOG_LEVEL,
            "PROXY_COUNT": self.PROXY_COUNT,
            "REQUEST_TIMEOUT_SEC": self.REQUEST_TIMEOUT_SEC,
            "MAX_CONTENT_LENGTH": self.MAX_CONTENT_LENGTH,
            "LT_BASE_URL": self.LT_BASE_URL,
            "LT_API_KEY": bool(self.LT_API_KEY),
            "ALLOWED_LANGS": list(self.ALLOWED_LANGS),
            "READY_MIN_LANGS": list(self.READY_MIN_LANGS),
            "DEFAULT_TARGETS_SOURCE": self.DEFAULT_TARGETS_SOURCE,
            "RATE_LIMIT_ENABLED": self.RATE_LIMIT_ENABLED,
            "RATE_BUCKET": self.RATE_BUCKET,
            "RATE_REFILL": self.RATE_REFILL,
            "CACHE_ENABLED": self.CACHE_ENABLED,
            "CACHE_TTL_SEC": self.CACHE_TTL_SEC,
            "CACHE_MAX_ITEMS": self.CACHE_MAX_ITEMS,
            "CACHE_VERSION": self.CACHE_VERSION,
            "REDIS_URL": bool(self.REDIS_URL),
            "CORS_ORIGINS": self.CORS_ORIGINS,
            "JSON_SORT_KEYS": self.JSON_SORT_KEYS,
            "JSONIFY_PRETTYPRINT_REGULAR": self.JSONIFY_PRETTYPRINT_REGULAR,
            "MAX_CHARS": self.MAX_CHARS,
            "MAX_TARGETS": self.MAX_TARGETS,
            "WAIT_FOR_UPSTREAM": self.WAIT_FOR_UPSTREAM,
            "READY_PROBE_INTERVAL_SEC": self.READY_PROBE_INTERVAL_SEC,
            "READY_START_DELAY_SEC": self.READY_START_DELAY_SEC,
            "UPSTREAM_TIMEOUT_SEC": self.UPSTREAM_TIMEOUT_SEC,
            "LT_INSTALL_LANGS": list(self.LT_INSTALL_LANGS),
            "INSTALL_STATUS_SOURCE": self.INSTALL_STATUS_SOURCE,
            "FS_PROBE_ENABLED": self.FS_PROBE_ENABLED,
            "LT_DATA_MOUNT": self.LT_DATA_MOUNT,
        }
