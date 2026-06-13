# services/language/core/ratelimit.py
"""
Token-Bucket Rate Limiter für translate-svc.

Eigenschaften
- Backends: Noop, InMemory, Redis (atomar via Lua).
- API: limiter.allow(key, weight=1) -> (allowed, retry_after_sec, remaining_tokens)
- Robuste Fallbacks: Limiter darf nie die Anfrage hart crashen.
- Konfigurierbar über App-Config:
    RATE_LIMIT_ENABLED: bool
    RATE_BUCKET: int           # Kapazität (Tokens)
    RATE_REFILL: int           # Refill-Rate pro Sekunde
    REDIS_URL: str|None        # optional; wenn gesetzt -> Redis-Backend
    SERVICE_NAME: str          # Namespace

Best-Practice
- Keybildung: pro Client-ID oder IP und optional pro Endpunkt.
  Beispiel: f"client:{client_id}" oder f"ip:{ip}|route:/translate"
"""

from __future__ import annotations

import math
import time
import logging
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping, Optional, Tuple

# Optionales Redis (wir koppeln nicht hart an cache.py)
try:  # pragma: no cover
    import redis  # type: ignore
except Exception:  # pragma: no cover
    redis = None  # type: ignore


# -----------------------------------------------------------------------------
# Datentyp
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_sec: float
    remaining: int


# -----------------------------------------------------------------------------
# Basisinterface
# -----------------------------------------------------------------------------

class BaseLimiter:
    def allow(self, key: str, weight: int = 1) -> Decision:  # pragma: no cover - Interface
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - Interface
        pass


class NoopLimiter(BaseLimiter):
    """Deaktiviert. Lässt alles zu."""

    def allow(self, key: str, weight: int = 1) -> Decision:
        return Decision(True, 0.0, 2**31 - 1)


# -----------------------------------------------------------------------------
# In-Memory Token Bucket
# -----------------------------------------------------------------------------

class MemoryTokenBucketLimiter(BaseLimiter):
    """
    Thread-sicherer Token-Bucket.
    - tokens: aktuelle Tokens
    - ts: letzte Auffüllzeit (monotonic)
    - capacity: RATE_BUCKET
    - refill: RATE_REFILL Tokens/Sekunde
    """

    def __init__(self, *, capacity: int, refill_per_sec: float, namespace: str = "translate-svc:rl", max_keys: int = 100_000):
        self.capacity = max(1, int(capacity))
        self.refill = max(0.01, float(refill_per_sec))
        self.ns = str(namespace or "translate-svc:rl")
        self.max_keys = max(1, int(max_keys))
        self._store: dict[str, tuple[float, float]] = {}  # key -> (tokens, ts)
        self._lock = RLock()
        self._log = logging.getLogger(__name__)

        # Bereinigen (Lazy), TTL ~ 2x Bucket-Auffüllfenster
        self._idle_ttl = max(60.0, 2.0 * (self.capacity / self.refill))
        self._last_prune = time.monotonic()

    def _now(self) -> float:
        return time.monotonic()

    def _prune(self, now: float) -> None:
        # selten aufräumen
        if now - self._last_prune < 30.0:
            return
        self._last_prune = now
        try:
            to_del = []
            for k, (_, ts) in self._store.items():
                if now - ts > self._idle_ttl:
                    to_del.append(k)
            for k in to_del:
                self._store.pop(k, None)
            # Hard-Limit gegen Key-Explosion
            if len(self._store) > self.max_keys:
                # grobe Hälfte entfernen: älteste nach ts
                items = sorted(self._store.items(), key=lambda it: it[1][1])
                for k, _ in items[: len(items) // 2]:
                    self._store.pop(k, None)
        except Exception:
            pass

    def _fullkey(self, key: str) -> str:
        return f"{self.ns}:{key}"

    def allow(self, key: str, weight: int = 1) -> Decision:
        if weight <= 0:
            return Decision(True, 0.0, self.capacity)

        k = self._fullkey(key)
        now = self._now()

        with self._lock:
            tokens, ts = self._store.get(k, (float(self.capacity), now))
            # Auffüllen
            elapsed = max(0.0, now - ts)
            if elapsed > 0.0:
                tokens = min(float(self.capacity), tokens + elapsed * self.refill)
                ts = now

            if tokens >= float(weight):
                tokens -= float(weight)
                self._store[k] = (tokens, ts)
                self._prune(now)
                return Decision(True, 0.0, max(0, int(tokens)))
            # Ablehnen: Retry-After berechnen
            needed = float(weight) - tokens
            retry_after = needed / self.refill if self.refill > 0 else 1.0
            # Speichern ohne Abzug
            self._store[k] = (tokens, ts)
            self._prune(now)
            return Decision(False, retry_after, max(0, int(tokens)))


# -----------------------------------------------------------------------------
# Redis Token Bucket (atomar via Lua)
# -----------------------------------------------------------------------------

_LUA_TOKEN_BUCKET = """
-- KEYS[1] = bucket key
-- ARGV[1] = now_ms
-- ARGV[2] = capacity (int)
-- ARGV[3] = refill_per_sec (float)
-- ARGV[4] = weight (int)
-- ARGV[5] = ttl_sec (int)
local key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local refill = tonumber(ARGV[3])
local weight = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if not tokens or not ts then
  tokens = cap
  ts = now_ms
end

-- refill
local elapsed = math.max(0, now_ms - ts) / 1000.0
tokens = math.min(cap, tokens + elapsed * refill)
ts = now_ms

local allowed = 0
local retry_after = 0.0
if tokens >= weight then
  tokens = tokens - weight
  allowed = 1
else
  retry_after = (weight - tokens) / refill
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', ts)
if ttl and ttl > 0 then
  redis.call('EXPIRE', key, ttl)
end

return {allowed, tokens, retry_after}
"""

class RedisTokenBucketLimiter(BaseLimiter):
    def __init__(self, *, url: str, capacity: int, refill_per_sec: float, namespace: str = "translate-svc:rl"):
        self.capacity = max(1, int(capacity))
        self.refill = max(0.01, float(refill_per_sec))
        self.ns = str(namespace or "translate-svc:rl")
        self._log = logging.getLogger(__name__)
        self._r = None
        self._script = None
        if redis is None:
            self._log.warning("redis_package_not_installed")
            return
        try:
            self._r = redis.Redis.from_url(url, decode_responses=True)
            # Script vorbereiten
            self._script = self._r.register_script(_LUA_TOKEN_BUCKET)
        except Exception as exc:
            self._log.warning("redis_init_failed: %s", exc)
            self._r = None
            self._script = None

        # TTL: 2x Zeit um Bucket voll zu füllen
        self._ttl = int(max(60.0, 2.0 * (self.capacity / self.refill)))

    def _fullkey(self, key: str) -> str:
        return f"{self.ns}:{key}"

    def allow(self, key: str, weight: int = 1) -> Decision:
        if self._r is None or self._script is None:
            # Fallback: durchlassen, nicht blockieren
            return Decision(True, 0.0, self.capacity)

        if weight <= 0:
            return Decision(True, 0.0, self.capacity)

        now_ms = int(time.time() * 1000)
        try:
            res = self._script(keys=[self._fullkey(key)],
                               args=[now_ms, self.capacity, self.refill, int(weight), self._ttl])
            # res = {allowed(int), tokens(float), retry_after(float)}
            allowed = bool(int(res[0]))
            tokens = float(res[1])
            retry_after = float(res[2])
            return Decision(allowed, max(0.0, retry_after), max(0, int(tokens)))
        except Exception as exc:
            # Redis-Fehler dürfen nicht blockieren
            self._log.warning("redis_rl_error: %s", exc)
            return Decision(True, 0.0, self.capacity)

    def close(self) -> None:
        try:
            if self._r is not None:
                self._r.close()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Factory + Helfer
# -----------------------------------------------------------------------------

def build_rate_limiter_from_config(config: Mapping[str, Any]) -> BaseLimiter:
    """
    Erstellt Limiter anhand App-Config.
    """
    def _cfg(name: str, default: Any) -> Any:
        try:
            return config.get(name, default)  # type: ignore
        except Exception:
            return default

    enabled = bool(_cfg("RATE_LIMIT_ENABLED", False))
    if not enabled:
        return NoopLimiter()

    capacity = max(1, int(_cfg("RATE_BUCKET", 60)))
    refill = max(0.01, float(_cfg("RATE_REFILL", 1)))
    ns = str(_cfg("SERVICE_NAME", "translate-svc")) + ":rl"
    rurl = _cfg("REDIS_URL", None)

    if rurl:
        rl = RedisTokenBucketLimiter(url=str(rurl), capacity=capacity, refill_per_sec=refill, namespace=ns)
        # Wenn Redis nicht init, fallback auf Memory
        if getattr(rl, "_r", None) is not None and getattr(rl, "_script", None) is not None:
            return rl
        logging.getLogger(__name__).warning("rate_limit_redis_unavailable_fallback_memory")

    return MemoryTokenBucketLimiter(capacity=capacity, refill_per_sec=refill, namespace=ns)


def build_key(ip: Optional[str], client_id: Optional[str], route: Optional[str] = None) -> str:
    """
    Bildet einen robusten Schlüssel:
      client:{id} | ip:{ip}  [+ "|route:{route}"]
    """
    base = None
    if client_id:
        base = f"client:{client_id}"
    elif ip:
        base = f"ip:{ip}"
    else:
        base = "ip:unknown"
    if route:
        base += f"|route:{route}"
    return base


# -----------------------------------------------------------------------------
# Flask-Integration (optional)
# -----------------------------------------------------------------------------

def limit_request_or_none(request, limiter: BaseLimiter, *, weight: int = 1) -> Tuple[Optional[Decision], dict]:
    """
    Nutzt Standard-Header zur Identifikation:
    - X-Client-ID bevorzugt
    - Fallback: Remote Addr
    Gibt (decision|None, headers), wobei headers Retry-After usw. enthalten.
    """
    hdrs = {}
    try:
        client_id = (request.headers.get("X-Client-ID") or "").strip() or None
        ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip() or None
        route = request.path
        key = build_key(ip, client_id, route)
        dec = limiter.allow(key, max(1, int(weight)))
        if not dec.allowed:
            hdrs["Retry-After"] = f"{max(1, int(math.ceil(dec.retry_after_sec)))}"
            hdrs["X-RateLimit-Remaining"] = str(max(0, dec.remaining))
        else:
            hdrs["X-RateLimit-Remaining"] = str(max(0, dec.remaining))
        return dec, hdrs
    except Exception as exc:
        logging.getLogger(__name__).warning("limit_request_failed: %s", exc)
        return None, hdrs
