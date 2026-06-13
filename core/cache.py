# services/language/core/cache.py
"""
Cache-Adapter für translate-svc.

Ziele
------
- Einheitliches Interface (get/set/delete/close/stats).
- InMemory-LRU mit TTL (thread-safe).
- Optional Redis-Backend (wenn REDIS_URL gesetzt und redis-Paket verfügbar).
- Stabile Schlüsselbildung für Übersetzungen (SHA-256 über normierte Inputs).
- Niemals Exceptions aus dem Cache nach außen leaken.

Hinweise
--------
- Werte werden JSON-serialisiert gespeichert (auch im InMemory-Cache), um Parität zu Redis zu wahren.
- TTL: 0/None -> nutzt default_ttl. Negative TTL wird ignoriert und als default_ttl behandelt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from threading import RLock
from typing import Any, Dict, Mapping, MutableMapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# Optionales Redis
try:  # pragma: no cover
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore


# ---------------------------------------------------------------------------
# Utilities: Sprachcode-Normalisierung und Key-Bildung
# ---------------------------------------------------------------------------

try:
    # Bevorzugt zentrale Sprachlogik verwenden
    from core.languages import canon_lang as _core_canon_lang, canon_targets as _core_canon_targets  # type: ignore
except Exception:  # pragma: no cover

    def _core_canon_lang(code: str) -> str:
        """Minimaler Fallback für Sprachcode-Normalisierung."""
        c = (code or "").strip().replace("_", "-")
        if not c:
            return c
        low = c.lower()
        if low in ("zh", "zh-cn", "zh-hans"):
            return "zh-CN"
        if low in ("zh-tw", "zh-hant", "zh-hk"):
            return "zh-TW"
        if len(low) == 5 and low[2] == "-":
            base, region = low.split("-", 1)
            if base == "zh":
                return f"{base}-{region.upper()}"
            return base
        return low

    def _core_canon_targets(items: Sequence[str]) -> Tuple[str, ...]:
        seen = set()
        out: list[str] = []
        for x in items or []:
            cx = _core_canon_lang(x)
            if cx and cx not in seen:
                seen.add(cx)
                out.append(cx)
        return tuple(out)


def _canon_lang(code: str) -> str:
    """
    Wrapper um core.languages.canon_lang mit defensivem Fallback.
    """
    try:
        return _core_canon_lang(code)
    except Exception:  # pragma: no cover
        log.warning("cache_canon_lang_failed", exc_info=True)
        return _core_canon_lang(code)


def _canon_targets(items: Sequence[str]) -> Tuple[str, ...]:
    """
    Wrapper um core.languages.canon_targets mit defensivem Fallback.
    """
    try:
        return _core_canon_targets(items)
    except Exception:  # pragma: no cover
        log.warning("cache_canon_targets_failed", exc_info=True)
        return _core_canon_targets(items)


def _json_dumps(obj: Any) -> str:
    """
    Serialisiert Werte robust zu JSON-Strings.

    - nutzt ensure_ascii=False und kompakte Separatoren
    - fällt auf str(obj) zurück, falls JSON-Serialisierung fehlschlägt.
    """
    try:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        try:
            return str(obj)
        except Exception:  # pragma: no cover
            return ""


def make_translation_key(
    *,
    source: str,
    targets: Sequence[str],
    format: str,
    text: str,
    alternatives: int = 0,
    version: int = 1,
) -> str:
    """
    Liefert stabilen Schlüssel für eine Übersetzungsanfrage.

    - Normalisiert source/targets (BCP-47-ähnlich).
    - Sortiert targets deterministisch.
    - Hash über relevanten Feldern (inkl. vollständigem Text).

    Parameter
    ---------
    source:
        Ursprungs-Sprache oder "auto".
    targets:
        Zielsprachen (werden kanonisiert + dedupliziert).
    format:
        "text" oder "html".
    text:
        Vollständiger Textinhalt (wird gehasht, nicht im Klartext gespeichert).
    alternatives:
        Anzahl gewünschter Alternativen (>= 0).
    version:
        Versionsnummer des Cache-Schemas (z. B. aus CACHE_VERSION).
    """
    try:
        src = _canon_lang(source or "auto")
        tgt = sorted(set(_canon_targets(targets)))
        fmt = "html" if str(format or "text").lower() == "html" else "text"
        try:
            alt = max(0, int(alternatives or 0))
        except Exception:
            alt = 0

        payload = {"s": src, "t": tgt, "f": fmt, "a": alt, "q": text}
        raw = _json_dumps(payload)
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        try:
            v = int(version)
        except Exception:
            v = 1
        return f"v{v}:{h}"
    except Exception:  # pragma: no cover
        # Letzter Fallback: sehr grober Key; besser als Exception werfen.
        log.warning("make_translation_key_failed", exc_info=True)
        return f"v1:{hash((source, tuple(targets), format, alternatives, text))}"


# ---------------------------------------------------------------------------
# Cache-Interface
# ---------------------------------------------------------------------------


class BaseCache:
    """
    Abstraktes Cache-Interface.

    Implementierungen müssen:
      - get(key) -> Optional[Any]
      - set(key, value, ttl_sec)
      - delete(key)
      - stats() -> Mapping[str, Any]
    bereitstellen und dabei niemals Exceptions nach außen werfen.
    """

    def get(self, key: str) -> Optional[Any]:  # pragma: no cover - Interface
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> None:  # pragma: no cover - Interface
        raise NotImplementedError

    def delete(self, key: str) -> None:  # pragma: no cover - Interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - Interface
        pass

    def stats(self) -> Mapping[str, Any]:
        return {}


class NoopCache(BaseCache):
    """Deaktivierter Cache, tut nichts, wirft nie Exceptions."""

    def __init__(self) -> None:
        self._hits = 0
        self._miss = 0

    def get(self, key: str) -> Optional[Any]:
        try:
            self._miss += 1
        except Exception:  # pragma: no cover
            pass
        return None

    def set(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> None:
        return

    def delete(self, key: str) -> None:
        return

    def stats(self) -> Mapping[str, Any]:
        return {"backend": "noop", "hits": self._hits, "misses": self._miss}


# ---------------------------------------------------------------------------
# InMemory LRU + TTL
# ---------------------------------------------------------------------------


class InMemoryTTLCache(BaseCache):
    """
    Thread-sicherer InMemory-Cache mit LRU und TTL.

    Eigenschaften
    -------------
    - Speichert JSON-Strings (Parität zu Redis).
    - Entfernt abgelaufene Einträge lazy bei get/set und bei Größen-Trim.
    - Namespaced Keys (namespace:key).
    """

    def __init__(self, *, max_items: int = 10_000, default_ttl: int = 3600, namespace: str = "translate-svc") -> None:
        self._store: "OrderedDict[str, Tuple[float, str]]" = OrderedDict()
        self._lock = RLock()
        self._max = max(1, int(max_items))
        self._ttl = max(1, int(default_ttl))
        self._ns = str(namespace or "translate-svc")
        self._hits = 0
        self._miss = 0
        self._evict = 0
        self._expired = 0
        self._log = logging.getLogger(__name__)

    def _now(self) -> float:
        return time.time()

    def _ns_key(self, key: str) -> str:
        return f"{self._ns}:{key}"

    def _trim(self) -> None:
        """
        Entfernt abgelaufene Einträge und trimmt auf max_items.
        """
        try:
            now = self._now()
            to_del = []
            for k, (exp, _) in list(self._store.items()):
                if exp > 0 and exp <= now:
                    to_del.append(k)
            for k in to_del:
                try:
                    del self._store[k]
                    self._expired += 1
                except Exception:
                    pass

            # LRU-Trim
            while len(self._store) > self._max:
                try:
                    self._store.popitem(last=False)
                    self._evict += 1
                except Exception:
                    break
        except Exception:  # pragma: no cover
            self._log.warning("memory_cache_trim_failed", exc_info=True)

    def get(self, key: str) -> Optional[Any]:
        k = self._ns_key(key)
        try:
            with self._lock:
                item = self._store.get(k)
                if not item:
                    self._miss += 1
                    return None
                exp, raw = item
                if exp > 0 and exp <= self._now():
                    # abgelaufen
                    try:
                        del self._store[k]
                    except Exception:
                        pass
                    self._expired += 1
                    self._miss += 1
                    return None
                # LRU: nach hinten
                try:
                    self._store.move_to_end(k, last=True)
                except Exception:
                    pass
                self._hits += 1
            # JSON zurückwandeln
            try:
                return json.loads(raw)
            except Exception:
                return None
        except Exception:
            # niemals Fehler nach außen leaken
            try:
                self._miss += 1
            except Exception:
                pass
            return None

    def set(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> None:
        k = self._ns_key(key)
        try:
            raw = _json_dumps(value)
            try:
                ttl = int(ttl_sec if ttl_sec is not None else self._ttl)
            except Exception:
                ttl = self._ttl
            if ttl <= 0:
                ttl = self._ttl
            exp = self._now() + ttl if ttl > 0 else 0

            with self._lock:
                self._store[k] = (exp, raw)
                try:
                    self._store.move_to_end(k, last=True)
                except Exception:
                    pass
                self._trim()
        except Exception:
            # still sein
            self._log.warning("memory_cache_set_failed", exc_info=True)
            return

    def delete(self, key: str) -> None:
        k = self._ns_key(key)
        try:
            with self._lock:
                if k in self._store:
                    del self._store[k]
        except Exception:
            return

    def stats(self) -> Mapping[str, Any]:
        try:
            with self._lock:
                return {
                    "backend": "memory",
                    "namespace": self._ns,
                    "size": len(self._store),
                    "max_items": self._max,
                    "hits": self._hits,
                    "misses": self._miss,
                    "evictions": self._evict,
                    "expired": self._expired,
                    "default_ttl_sec": self._ttl,
                }
        except Exception:  # pragma: no cover
            return {"backend": "memory", "error": "stats_failed"}


# ---------------------------------------------------------------------------
# Redis-Adapter
# ---------------------------------------------------------------------------


class RedisCache(BaseCache):
    """
    Redis-Backend mit JSON-Serialisierung.

    Eigenschaften
    -------------
    - Nutzt decode_responses=True (UTF-8 Strings).
    - set() nutzt EX (TTL in Sek.) für Ablauffrist.
    - Verhält sich im Fehlerfall wie ein NoopCache (Fehler werden geloggt, nicht propagiert).
    """

    def __init__(self, *, url: str, default_ttl: int = 3600, namespace: str = "translate-svc") -> None:
        self._ns = str(namespace or "translate-svc")
        self._ttl = max(1, int(default_ttl))
        self._hits = 0
        self._miss = 0
        self._log = logging.getLogger(__name__)
        self._url = url
        self._r = None
        if redis is None:
            self._log.warning("redis_package_not_installed")
            return
        try:
            self._r = redis.Redis.from_url(url, decode_responses=True)
            # Sanity-Check
            self._r.ping()
        except Exception as exc:
            self._log.warning("redis_unavailable: %s", exc)
            self._r = None

    def _ns_key(self, key: str) -> str:
        return f"{self._ns}:{key}"

    def get(self, key: str) -> Optional[Any]:
        if self._r is None:
            self._miss += 1
            return None
        try:
            val = self._r.get(self._ns_key(key))
            if val is None:
                self._miss += 1
                return None
            self._hits += 1
            try:
                return json.loads(val)
            except Exception:
                return None
        except Exception as exc:
            self._log.warning("redis_get_failed: %s", exc)
            self._miss += 1
            return None

    def set(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> None:
        if self._r is None:
            return
        try:
            raw = _json_dumps(value)
            try:
                ttl = int(ttl_sec if ttl_sec is not None else self._ttl)
            except Exception:
                ttl = self._ttl
            if ttl <= 0:
                ttl = self._ttl
            self._r.set(self._ns_key(key), raw, ex=ttl)
        except Exception as exc:
            self._log.warning("redis_set_failed: %s", exc)

    def delete(self, key: str) -> None:
        if self._r is None:
            return
        try:
            self._r.delete(self._ns_key(key))
        except Exception as exc:
            self._log.warning("redis_del_failed: %s", exc)

    def close(self) -> None:
        try:
            if self._r is not None:
                self._r.close()
        except Exception:
            pass

    def stats(self) -> Mapping[str, Any]:
        if self._r is None:
            return {"backend": "redis", "connected": False, "hits": self._hits, "misses": self._miss}
        try:
            inf: Dict[str, Any] = {}
            try:
                # leichtgewichtige Infos
                mi = self._r.info(section="memory") or {}
                inf = {
                    "used_memory": mi.get("used_memory_human") or mi.get("used_memory"),
                    "maxmemory": mi.get("maxmemory_human") or mi.get("maxmemory"),
                }
            except Exception:
                inf = {}
            return {
                "backend": "redis",
                "connected": True,
                "namespace": self._ns,
                "hits": self._hits,
                "misses": self._miss,
                "default_ttl_sec": self._ttl,
                "info": inf,
            }
        except Exception:  # pragma: no cover
            return {"backend": "redis", "connected": True, "error": "stats_failed"}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_cache_from_config(config: Mapping[str, Any]) -> BaseCache:
    """
    Erstellt passenden Cache anhand App-Config.

    Erwartete Keys
    --------------
      - CACHE_ENABLED: bool
      - CACHE_TTL_SEC: int
      - REDIS_URL: str|None
      - SERVICE_NAME: optional für Namespace
      - CACHE_MAX_ITEMS: optional (nur InMemory)
    """
    logger = logging.getLogger(__name__)

    def _cfg(name: str, default: Any) -> Any:
        try:
            # flask app.config ist Mapping[str, Any]
            return config.get(name, default)  # type: ignore[return-value]
        except Exception:
            return default

    enabled = bool(_cfg("CACHE_ENABLED", False))
    if not enabled:
        return NoopCache()

    try:
        ttl = int(_cfg("CACHE_TTL_SEC", 3600))
    except Exception:
        ttl = 3600

    ns = str(_cfg("SERVICE_NAME", "translate-svc"))
    rurl = _cfg("REDIS_URL", None)
    try:
        max_items = int(_cfg("CACHE_MAX_ITEMS", int(os.getenv("CACHE_MAX_ITEMS", "10000"))))
    except Exception:
        max_items = 10_000

    if rurl:
        c = RedisCache(url=str(rurl), default_ttl=ttl, namespace=ns)
        # Wenn Redis-Client nicht verfügbar, auf Memory zurückfallen
        if isinstance(c, RedisCache) and getattr(c, "_r", None) is not None:
            logger.info("cache_backend=redis namespace=%s ttl=%s", ns, ttl)
            return c
        logger.warning("redis_not_ready_fallback_memory namespace=%s", ns)

    logger.info("cache_backend=memory namespace=%s ttl=%s max_items=%s", ns, ttl, max_items)
    return InMemoryTTLCache(max_items=max_items, default_ttl=ttl, namespace=ns)
