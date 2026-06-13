# services/language/routes/translate.py
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from flask import Blueprint, Response, current_app, jsonify, request

# ---------- Fehler-API (Fallback, falls core.errors fehlt) ----------
try:
    from core.errors import (
        APIError,
        BadRequest,
        ValidationError,
        PayloadTooLarge,
        TooManyRequests,
        as_response,
        map_exception,
    )
except Exception:  # pragma: no cover
    class APIError(Exception):
        status = 400
        code = "api_error"

        def __init__(
            self,
            status: int,
            code: str,
            message: str,
            details: Optional[Mapping[str, Any]] = None,
        ):
            self.status = status
            self.code = code
            self.message = message
            self.details = dict(details or {})
            super().__init__(message)

    class BadRequest(APIError):
        def __init__(self, message: str = "bad_request", details: Optional[Mapping[str, Any]] = None):
            super().__init__(400, "bad_request", message, details)

    class ValidationError(APIError):
        def __init__(self, message: str = "invalid_argument", details: Optional[Mapping[str, Any]] = None):
            super().__init__(400, "invalid_argument", message, details)

    class PayloadTooLarge(APIError):
        def __init__(self, message: str = "payload_too_large", details: Optional[Mapping[str, Any]] = None):
            super().__init__(413, "payload_too_large", message, details)

    class TooManyRequests(APIError):
        def __init__(self, message: str = "rate_limited", details: Optional[Mapping[str, Any]] = None):
            super().__init__(429, "rate_limited", message, details)

    def as_response(err: APIError):
        return (
            jsonify({"error": {"code": err.code, "message": err.message, "details": err.details}}),
            err.status,
        )

    def map_exception(exc: Exception) -> APIError:
        return APIError(500, "internal_error", "unexpected_error", {"type": type(exc).__name__})


# ---------- Zentrale Sprachlogik ----------
try:
    from core.languages import canon_lang as _canon_lang, canon_targets as _canon_targets
except Exception:  # pragma: no cover
    # Minimaler Fallback (sollte in Praxis nicht greifen)
    def _canon_lang(code: str) -> str:
        c = (code or "").strip().replace("_", "-").lower()
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

    def _canon_targets(items: Iterable[str]) -> Tuple[str, ...]:
        seen = set()
        out: List[str] = []
        for x in items:
            cx = _canon_lang(x)
            if cx and cx not in seen:
                seen.add(cx)
                out.append(cx)
        return tuple(out)


# ---------- Schemas (DTOs + Validierung) ----------
from core.schemas import (
    parse_translate_request,
    build_translate_response,
    asdict_response,
)

# ---------- Upstream-Client ----------
try:
    from clients.http import HttpClientConfig
    from clients.libretranslate import build_client_from_config
except Exception as _imp_exc:  # pragma: no cover
    raise RuntimeError(f"client_modules_missing: {type(_imp_exc).__name__}") from _imp_exc

# ---------- Cache-Key-Helfer ----------
try:
    from core.cache import make_translation_key  # type: ignore
except Exception:  # pragma: no cover

    def make_translation_key(
        *,
        source: str,
        targets: Sequence[str],
        format: str,
        text: str,
        alternatives: int = 0,
        version: int = 1,
    ) -> str:
        # sehr einfacher Fallback
        return f"v{version}:{source}|{','.join(sorted(targets))}|{format}|{alternatives}|{hash(text)}"


bp = Blueprint("translate", __name__)
log = logging.getLogger(__name__)


# ---------- interne Helfer ----------


def _safe_str(obj: Any) -> str:
    try:
        if obj is None:
            return ""
        return str(obj)
    except Exception:  # pragma: no cover
        try:
            return repr(obj)
        except Exception:
            return ""


# ---------- Cache ----------


def _get_cache():
    try:
        c = current_app.extensions.get("translate_cache")
        if c is None:
            from core.cache import build_cache_from_config  # type: ignore

            c = build_cache_from_config(current_app.config)
            current_app.extensions["translate_cache"] = c
        return c
    except Exception:
        return None


def _cache_get(key: str):
    try:
        cache = _get_cache()
        return cache.get(key) if cache else None
    except Exception:
        return None


def _cache_set(key: str, value: Any, ttl: Optional[int] = None) -> None:
    try:
        cache = _get_cache()
        if cache:
            cache.set(key, value, ttl_sec=ttl)
    except Exception:
        return


# ---------- Optionales Rate-Limit ----------


def _get_rate_limiter():
    try:
        rl = current_app.extensions.get("rate_limiter")
        if rl is None:
            from core.ratelimit import build_rate_limiter_from_config  # type: ignore

            rl = build_rate_limiter_from_config(current_app.config)
            current_app.extensions["rate_limiter"] = rl
        return rl
    except Exception:
        return None


def _apply_rate_limit_or_pass() -> Tuple[Optional[object], Dict[str, str]]:
    """
    Gibt (decision|None, headers) zurück.
    Bei Ablehnung wird der Fehler im Handler beantwortet.
    """
    try:
        from core.ratelimit import limit_request_or_none  # type: ignore

        limiter = _get_rate_limiter()
        if not limiter:
            return None, {}
        dec, hdrs = limit_request_or_none(request, limiter, weight=1)
        return dec, hdrs
    except Exception:
        return None, {}


# ---------- Chunking ----------


def _maybe_chunk_text(text: str, limit: int, *, fmt: str) -> List[str]:
    """
    Chunking für große Texte:
    - wenn len(text) <= limit: [text]
    - HTML wird nicht gechunkt; bei Überschreitung → PayloadTooLarge
    - Text nutzt core.chunking.smart_chunks, mit robustem Fallback.
    """
    if len(text) <= limit:
        return [text]

    if fmt == "html":
        raise PayloadTooLarge("html_too_large", {"max_chars": limit, "got": len(text)})

    try:
        from core.chunking import smart_chunks  # type: ignore

        chunks = list(smart_chunks(text, limit=limit))
        if chunks:
            return chunks
    except Exception:
        # Fallback: einfache Heuristik
        pass

    chunks: List[str] = []
    remain = text
    seps = ["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " "]
    while remain:
        if len(remain) <= limit:
            chunks.append(remain)
            break
        cut = -1
        for sep in seps:
            idx = remain.rfind(sep, 0, limit)
            if idx > cut:
                cut = idx + len(sep)
            if cut >= 0:
                break
        if cut <= 0:
            cut = limit
        chunks.append(remain[:cut])
        remain = remain[cut:]
    return chunks


def _merge_translations(chunks_by_lang: Dict[str, List[str]]) -> Dict[str, str]:
    """
    Führt chunkweise Übersetzungen je Sprache wieder zu einem Gesamtstring zusammen.
    """
    return {lang: "".join(parts) for lang, parts in chunks_by_lang.items()}


# ---------- Upstream-Verfügbarkeit ----------


def _snapshot_available_from_monitor() -> Optional[Tuple[Tuple[str, ...], Optional[int]]]:
    st = current_app.extensions.get("upstream_state")  # type: ignore
    if not st:
        return None
    try:
        snap = st.snapshot()  # type: ignore[attr-defined]
        langs = tuple(sorted({str(c) for c in (snap.get("available_langs") or [])}))
        status = snap.get("last_status")
        return langs, (int(status) if status is not None else None)
    except Exception:
        return None


def _available_languages_live(allowed: Tuple[str, ...]) -> Tuple[Tuple[str, ...], Optional[int]]:
    """
    Fragt den Upstream direkt nach /languages ab und filtert gegen allowed.
    """
    cfg = HttpClientConfig(
        base_url=str(current_app.config.get("LT_BASE_URL", "http://libretranslate:5000")),
        timeout_sec=int(current_app.config.get("REQUEST_TIMEOUT_SEC", 30)),
    )
    api_key = current_app.config.get("LT_API_KEY")
    client = build_client_from_config(
        base_url=cfg.base_url,
        timeout_sec=cfg.timeout_sec,
        api_key=api_key,
        allowed_langs=allowed,
    )
    try:
        langs = client.languages()
        return tuple(sorted({str(x.code) for x in langs if getattr(x, "code", None)})), 200
    except Exception:
        return tuple(), None
    finally:
        client.close()


def _get_available_langs(allowed: Tuple[str, ...]) -> Tuple[Tuple[str, ...], Optional[int]]:
    """
    Liefert (available_langs, last_status) basierend auf Monitor-Snapshot oder Live-Call.
    """
    snap = _snapshot_available_from_monitor()
    if snap is not None:
        langs, status = snap
        if langs:
            langs = tuple([l for l in langs if l in set(allowed)])
            return langs, status
    langs, status = _available_languages_live(allowed)
    langs = tuple([l for l in langs if l in set(allowed)])
    return langs, status


# ---------- Fehlerantwort mit Retry-After ----------


def _error_with_retry(err: APIError, *, retry_after: Optional[int]) -> Response:
    resp, status = as_response(err)
    try:
        if retry_after and status in (429, 503):
            from flask import make_response

            r = make_response(resp, status) if not hasattr(resp, "headers") else resp
            r.headers["Retry-After"] = str(max(1, int(retry_after)))
            return r
    except Exception:
        pass
    return resp


# ---------- Endpunkte: Sprachen ----------


@bp.get("/languages")
def languages() -> Response:
    try:
        allowed = tuple(current_app.config.get("ALLOWED_LANGS", ())) or ()
        available, _status = _get_available_langs(allowed)
        data = [{"code": c, "name": None} for c in available]
        return jsonify({"languages": data, "count": len(data)})
    except Exception as exc:
        return as_response(map_exception(exc))


@bp.route("/languages", methods=["HEAD"])
def languages_head() -> Response:
    try:
        allowed = tuple(current_app.config.get("ALLOWED_LANGS", ())) or ()
        available, _ = _get_available_langs(allowed)
        resp = Response(status=200)
        resp.headers["X-Languages-Count"] = str(len(available))
        return resp
    except Exception:
        return Response(status=200)


@bp.get("/languages/plan")
def languages_plan() -> Response:
    """
    Liefert Installationsplan vs. Allowed-Sprachen.
    """
    try:
        allowed = tuple(_canon_lang(x) for x in (current_app.config.get("ALLOWED_LANGS", ()) or ()))
        plan = tuple(_canon_lang(x) for x in (current_app.config.get("LT_INSTALL_LANGS", ()) or ()))
        # nur erlaubte im Plan
        allow = set(allowed)
        plan = tuple(sorted([p for p in plan if p in allow or p.split("-")[0] in allow]))
        return jsonify(
            {
                "allowed": list(sorted(set(allowed))),
                "install_plan": list(plan),
                "count_allowed": len(set(allowed)),
                "count_plan": len(plan),
            }
        )
    except Exception as exc:
        return as_response(map_exception(exc))


# ---------- Endpunkt: /translate ----------


@bp.post("/translate")
def translate() -> Response:
    t0 = time.perf_counter()

    # Optionales Rate-Limit
    dec, rl_hdrs = _apply_rate_limit_or_pass()
    if dec is not None and getattr(dec, "allowed", True) is False:
        err = TooManyRequests("rate_limited")
        resp = _error_with_retry(err, retry_after=int(rl_hdrs.get("Retry-After", "1") or 1))
        try:
            resp.headers.update(rl_hdrs)  # type: ignore[attr-defined]
        except Exception:
            pass
        return resp

    try:
        payload = request.get_json(silent=True, force=False)
        if not isinstance(payload, Mapping):
            raise BadRequest("invalid_json")

        # ---------------- Konfiguration auslesen ----------------
        allowed: Tuple[str, ...] = tuple(current_app.config.get("ALLOWED_LANGS", ())) or ()
        default_targets_source = str(current_app.config.get("DEFAULT_TARGETS_SOURCE", "available")).lower()
        wait_for_upstream = (
            str(current_app.config.get("WAIT_FOR_UPSTREAM", "0")).strip().lower() in {"1", "true", "yes", "on"}
        )
        probe_interval = int(current_app.config.get("READY_PROBE_INTERVAL_SEC", 30))
        cache_ttl = int(current_app.config.get("CACHE_TTL_SEC", 3600))
        cache_version = int(current_app.config.get("CACHE_VERSION", 1) or 1)
        max_targets = int(current_app.config.get("MAX_TARGETS", 10))
        max_chars = int(current_app.config.get("MAX_CHARS", 10_000))
        ready_min_langs: Tuple[str, ...] = tuple(current_app.config.get("READY_MIN_LANGS", ())) or allowed

        # ---------------- Upstream-Verfügbarkeit ----------------
        available_now, last_status = _get_available_langs(allowed)
        avail_set = set(available_now)

        raw_targets = payload.get("targets", None)
        chosen_targets_source: str

        # Default-Targets bestimmen (bevor wir parse_translate_request aufrufen)
        if raw_targets is None:
            if default_targets_source == "allowed":
                default_targets_list: Sequence[str] = allowed
                chosen_targets_source = "allowed"
            elif default_targets_source == "minimum":
                default_targets_list = ready_min_langs
                chosen_targets_source = "minimum"
            else:  # "available" (Default)
                initial = tuple([t for t in allowed if t in avail_set])
                if initial:
                    default_targets_list = initial
                    chosen_targets_source = "available"
                else:
                    if wait_for_upstream:
                        err = APIError(
                            503,
                            "upstream_unavailable",
                            "translation_upstream_not_ready",
                            {
                                "available": list(available_now),
                                "required_min": list(
                                    current_app.config.get("READY_MIN_LANGS", ()) or allowed
                                ),
                                "last_status": last_status,
                            },
                        )
                        return _error_with_retry(err, retry_after=probe_interval)
                    # Fallback auf minimale Menge oder allowed
                    fallback = tuple([t for t in ready_min_langs if t in set(allowed)])
                    default_targets_list = fallback if fallback else allowed
                    chosen_targets_source = "fallback_minimum"
        else:
            # Client setzt Targets explizit
            default_targets_list = allowed  # wird von parse_translate_request ignoriert
            chosen_targets_source = "requested"

        # ---------------- Request parsen/validieren ----------------
        treq = parse_translate_request(
            payload,
            allowed_langs=allowed,
            default_targets=default_targets_list,
            max_targets=max_targets,
            max_chars=max_chars,
        )

        # Wenn der Client Targets explizit gesetzt hat, prüfen wir Upstream-Verfügbarkeit
        if raw_targets is not None:
            missing = [t for t in treq.targets if t not in avail_set]
            if missing:
                raise ValidationError(
                    "targets_not_available",
                    {"missing": missing, "available": list(available_now)},
                )

        # ---------------- Chunking ----------------
        chunks = _maybe_chunk_text(treq.text, max_chars, fmt=treq.format)

        # ---------------- Upstream-Client ----------------
        cfg = HttpClientConfig(
            base_url=str(current_app.config.get("LT_BASE_URL", "http://libretranslate:5000")),
            timeout_sec=int(current_app.config.get("REQUEST_TIMEOUT_SEC", 30)),
        )
        api_key = current_app.config.get("LT_API_KEY")
        client = build_client_from_config(
            base_url=cfg.base_url,
            timeout_sec=cfg.timeout_sec,
            api_key=api_key,
            allowed_langs=allowed,
        )

        alternatives = treq.alternatives
        translations_acc: Dict[str, List[str]] = {t: [] for t in treq.targets}
        alternatives_acc: Dict[str, List[str]] = {}
        used_source: Optional[str] = None

        cache_hits = 0
        cache_misses = 0

        log.info(
            "translate_start source=%s fmt=%s targets=%s chosen_source=%s avail_upstream=%s wait_strict=%s",
            treq.source,
            treq.format,
            list(treq.targets),
            chosen_targets_source,
            list(available_now),
            wait_for_upstream,
        )

        # ---------------- Auto-Detection (einmal pro Request) ----------------
        detected_source: Optional[str] = None
        if treq.source == "auto" and treq.text.strip():
            sample_text = treq.text[: min(len(treq.text), max_chars)]
            try:
                det = client.detect(sample_text, top_n=1, timeout_sec=cfg.timeout_sec)
                if det:
                    detected_source = det[0].language
            except Exception:
                detected_source = None

        if treq.source != "auto":
            effective_source = treq.source
        else:
            effective_source = detected_source or "auto"

        # ---------------- Übersetzen chunkweise mit Cache ----------------
        try:
            for chunk in chunks:
                cached_texts: Dict[str, Optional[Dict[str, Any]]] = {}
                missing_langs: List[str] = []

                # 1) Cache-Lookups pro Ziel
                for lang in treq.targets:
                    cache_source_key = _canon_lang(effective_source) if effective_source != "auto" else "auto"
                    key = make_translation_key(
                        source=cache_source_key,
                        targets=[lang],
                        format=treq.format,
                        text=chunk,
                        alternatives=alternatives,
                        version=cache_version,
                    )
                    val = _cache_get(key)
                    if isinstance(val, dict):
                        cached_texts[lang] = val
                        cache_hits += 1
                    else:
                        cached_texts[lang] = None
                        missing_langs.append(lang)
                        cache_misses += 1

                # 2) Upstream nur für fehlende Ziele
                results: Dict[str, Any] = {}
                if missing_langs:
                    results = client.translate_many_targets(
                        chunk,
                        source=effective_source,
                        targets=list(missing_langs),
                        format=treq.format,
                        alternatives=alternatives,
                        timeout_sec=cfg.timeout_sec,
                    )

                # 3) Zusammenführen + Cache befüllen
                for lang in treq.targets:
                    entry = cached_texts.get(lang)
                    if entry is None:
                        # frisch übersetzt?
                        r = results.get(lang)
                        if r is None:
                            translations_acc.setdefault(lang, []).append("")
                            continue

                        if used_source is None:
                            used_source = r.source or effective_source or "auto"

                        if r.source and _canon_lang(r.source) == lang:
                            # Quelle = Ziel → kein echter Übersetzungsschritt, Chunk übernehmen
                            translations_acc[lang].append(chunk)
                        else:
                            translations_acc[lang].append(r.translated or "")

                        if alternatives > 0 and r.alternatives:
                            alternatives_acc.setdefault(lang, [])
                            alternatives_acc[lang].extend([_safe_str(x) for x in (r.alternatives or [])])

                        # in Cache schreiben
                        try:
                            cache_src = r.source or effective_source or "auto"
                            cache_source_key = _canon_lang(cache_src) if cache_src != "auto" else "auto"
                            key = make_translation_key(
                                source=cache_source_key,
                                targets=[lang],
                                format=treq.format,
                                text=chunk,
                                alternatives=alternatives,
                                version=cache_version,
                            )
                            _cache_set(
                                key,
                                {
                                    "source": cache_source_key,
                                    "translated": r.translated or "",
                                    "alternatives": list(r.alternatives or []),
                                    "provider": r.provider,
                                    "duration_ms": r.duration_ms,
                                },
                                ttl=cache_ttl,
                            )
                        except Exception:
                            # Cache darf niemals Request hart crashen
                            pass
                    else:
                        # Cache-Hit
                        src = _safe_str(entry.get("source", effective_source or "auto"))
                        trn = _safe_str(entry.get("translated", ""))
                        alts = entry.get("alternatives", None)

                        if used_source is None:
                            used_source = src or "auto"

                        if src and _canon_lang(src) == lang:
                            translations_acc[lang].append(chunk)
                        else:
                            translations_acc[lang].append(trn)

                        if alternatives > 0 and isinstance(alts, list):
                            alternatives_acc.setdefault(lang, [])
                            alternatives_acc[lang].extend([_safe_str(x) for x in alts])
        finally:
            try:
                client.close()
            except Exception:
                pass

        translations = _merge_translations(translations_acc)

        # ---------------- Response bauen (Schemas) ----------------
        duration_ms = int((time.perf_counter() - t0) * 1000)
        base_resp = build_translate_response(
            source=used_source or effective_source or treq.source,
            translations=translations,
            duration_ms=duration_ms,
            provider="libretranslate",
            alternatives=alternatives_acc if (alternatives > 0 and alternatives_acc) else None,
        )
        resp_payload = asdict_response(base_resp)

        # Meta um zusätzliche Infos erweitern
        try:
            meta = resp_payload.get("meta", {}) or {}
            meta.update(
                {
                    "default_targets_source": default_targets_source,
                    "available_upstream": list(available_now),
                    "chosen_targets_source": chosen_targets_source,
                    "cache": {
                        "hits": cache_hits,
                        "misses": cache_misses,
                    },
                }
            )
            resp_payload["meta"] = meta
        except Exception:
            # Meta-Erweiterung darf Fehler nicht nach außen leaken
            pass

        resp = jsonify(resp_payload)
        try:
            if rl_hdrs:
                for k, v in rl_hdrs.items():
                    resp.headers[k] = v
        except Exception:
            pass
        return resp

    except APIError as err:
        ra = int(current_app.config.get("READY_PROBE_INTERVAL_SEC", 30)) if err.status in (429, 503) else None
        return _error_with_retry(err, retry_after=ra)
    except Exception as exc:
        log.exception("translate_unhandled_exception")
        return as_response(map_exception(exc))
