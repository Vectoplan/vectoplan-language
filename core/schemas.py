# services/language/core/schemas.py
"""
DTOs + Validierung für translate-svc, ohne externe Pflicht-Abhängigkeiten.

Funktionen
----------
- canon_lang / canon_targets: robuste Sprachcode-Normalisierung (BCP-47-ähnlich).
- parse_translate_request: validiert und normalisiert den Request-Body.
- build_translate_response: standardisiert die Antwortstruktur.

Hinweise
--------
- Wir verwenden core.languages als Single Source of Truth für Sprachcodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Fehlerklasse aus core.errors, mit Fallback
# ---------------------------------------------------------------------------

try:  # pragma: no cover
    from core.errors import ValidationError
except Exception:  # pragma: no cover

    class ValidationError(Exception):
        def __init__(
            self,
            message: str = "invalid_argument",
            details: Optional[Mapping[str, Any]] = None,
        ) -> None:
            super().__init__(message)
            self.status = 400
            self.code = "invalid_argument"
            self.message = message
            self.details = dict(details or {})


# ---------------------------------------------------------------------------
# Sprachlogik zentral aus core.languages
# ---------------------------------------------------------------------------

try:
    from core.languages import (
        canon_lang as _canon_lang,
        canon_targets as _canon_targets,
        DEFAULT_ALLOWED_LANGS as _DEFAULT_ALLOWED_LANGS,
    )
except Exception:
    # Minimaler Fallback (nur für sehr eingeschränkte Umgebungen).
    _DEFAULT_ALLOWED_LANGS = (
        "en",
        "de",
        "fr",
        "es",
        "pt",
        "ru",
        "zh-CN",
        "ja",
        "ko",
        "tr",
        "pl",
        "it",
        "nl",
        "ar",
        "id",
        "cs",
        "uk",
        "sv",
    )

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
        for x in items or []:
            cx = _canon_lang(x)
            if cx and cx not in seen:
                seen.add(cx)
                out.append(cx)
        return tuple(out)


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranslateRequest:
    text: str  # zusammengeführter Text (string|string[])
    source: str  # "auto" oder ISO (kanonisch)
    targets: Tuple[str, ...]  # Zielsprachen (kanonisch)
    format: str  # "text" | "html"
    alternatives: int  # >= 0


@dataclass(frozen=True)
class TranslateResponse:
    source: str
    translations: Mapping[str, str]
    alternatives: Optional[Mapping[str, List[str]]]
    meta: Mapping[str, Any]


# ---------------------------------------------------------------------------
# interne Helfer
# ---------------------------------------------------------------------------


def _safe_str(val: Any) -> str:
    """Defensive str()-Konvertierung."""
    try:
        if val is None:
            return ""
        return str(val)
    except Exception:  # pragma: no cover
        try:
            return repr(val)
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Parser/Validator
# ---------------------------------------------------------------------------


def _normalize_text(obj: Any) -> str:
    """
    Normalisiert das text-Feld des Requests.

    Erlaubt:
      - str
      - List[str] → wird mit '\n' zusammengeführt.

    Rückgabe:
      - Nicht-leerer String, sonst ValidationError("text_empty").
    """
    if isinstance(obj, str):
        txt = obj
    elif isinstance(obj, list):
        try:
            parts = [_safe_str(x) for x in obj if x is not None]
        except Exception:
            raise ValidationError("text_list_invalid")
        txt = "\n".join(parts)
    else:
        raise ValidationError("text_required_string_or_list")

    if isinstance(txt, str) and txt.strip() == "":
        # Leer oder nur Whitespace
        return ""
    return txt


def parse_translate_request(
    payload: Mapping[str, Any],
    *,
    allowed_langs: Sequence[str],
    default_targets: Optional[Sequence[str]] = None,
    max_targets: int = 10,
    max_chars: int = 10_000,
) -> TranslateRequest:
    """
    Validiert und normalisiert den Body für POST /translate.

    Regeln
    ------
    - payload muss Mapping sein, sonst ValidationError("invalid_json").
    - text: str oder str[], Pflicht. Länge <= max_chars, sonst ValidationError("text_too_large").
    - source: str, default "auto". Normalisiert (BCP-47-ähnlich). "auto" bleibt "auto".
    - format: "text" | "html", default "text".
    - targets:
        * Wenn nicht gesetzt → default_targets oder allowed_langs.
        * Normalisiert (canon_lang), auf allowed_langs gefiltert.
        * Wenn nach Filter nichts übrig → ValidationError("no_valid_targets").
        * max_targets wird nur geprüft, wenn der Client targets explizit gesetzt hat.
    - alternatives: int >= 0, default 0. Ungültige Werte werden auf 0 gesetzt.
    """
    if not isinstance(payload, Mapping):
        raise ValidationError("invalid_json")

    # ---------------- Text ----------------
    text_raw = payload.get("text")
    text = _normalize_text(text_raw)
    if not text:
        raise ValidationError("text_empty")

    try:
        mc = int(max_chars)
    except Exception:
        mc = 10_000
    mc = max(1, mc)

    if len(text) > mc:
        raise ValidationError("text_too_large", {"max_chars": mc, "got": len(text)})

    # ---------------- Source + Format ----------------
    source_raw = _safe_str(payload.get("source", "auto") or "auto")
    source = _canon_lang(source_raw) if source_raw.lower() != "auto" else "auto"

    fmt = _safe_str(payload.get("format", "text") or "text").lower()
    if fmt not in ("text", "html"):
        raise ValidationError("unsupported_format", {"format": fmt})

    # ---------------- Allowed-Sprachen vorbereiten ----------------
    try:
        if allowed_langs:
            allowed_norm = _canon_targets(allowed_langs)
        else:
            allowed_norm = _canon_targets(_DEFAULT_ALLOWED_LANGS)
    except Exception:
        allowed_norm = _canon_targets(_DEFAULT_ALLOWED_LANGS)

    if not allowed_norm:
        allowed_norm = _canon_targets(_DEFAULT_ALLOWED_LANGS)

    allowed_set = {c for c in allowed_norm if c}

    # ---------------- Targets ----------------
    raw_targets = payload.get("targets", None)
    client_set_explicitly = raw_targets is not None

    if raw_targets is None:
        # Fallback-Kette: default_targets -> allowed_langs -> DEFAULT_ALLOWED_LANGS
        base_seq: Sequence[str]
        if default_targets is not None and len(default_targets) > 0:
            base_seq = default_targets
        elif allowed_langs:
            base_seq = allowed_langs
        else:
            base_seq = _DEFAULT_ALLOWED_LANGS
        targets = _canon_targets(base_seq)
    else:
        if not isinstance(raw_targets, (list, tuple)):
            raise ValidationError("targets_must_be_list")
        targets = _canon_targets([_safe_str(x) for x in raw_targets])

    # Whitelist erzwingen
    filtered = tuple([t for t in targets if t in allowed_set])

    if not filtered:
        raise ValidationError("no_valid_targets", {"allowed": sorted(allowed_set)})

    # Limit nur anwenden, wenn Client Targets explizit gesetzt hat
    try:
        mt = int(max_targets)
    except Exception:
        mt = 10
    mt = max(1, mt)

    if client_set_explicitly and len(filtered) > mt:
        raise ValidationError("too_many_targets", {"max_targets": mt, "got": len(filtered)})

    # ---------------- Alternatives ----------------
    try:
        alt_raw = payload.get("alternatives", 0)
        alt = int(alt_raw or 0)
    except Exception:
        alt = 0
    if alt < 0:
        alt = 0

    return TranslateRequest(
        text=text,
        source=source,
        targets=filtered,
        format=fmt,
        alternatives=alt,
    )


# ---------------------------------------------------------------------------
# Response-Erstellung
# ---------------------------------------------------------------------------


def build_translate_response(
    *,
    source: str,
    translations: Mapping[str, str],
    duration_ms: int,
    provider: str = "libretranslate",
    alternatives: Optional[Mapping[str, List[str]]] = None,
    extra_meta: Optional[Mapping[str, Any]] = None,
) -> TranslateResponse:
    """
    Baut eine standardisierte Antwortstruktur.

    - source wird kanonisiert (sofern nicht "auto").
    - meta enthält mindestens: duration_ms, targets_count, provider.
    - extra_meta kann zusätzliche Felder beisteuern (z. B. Cache-/RL-Infos).
    """
    try:
        src = _canon_lang(source or "auto") if (source or "").lower() != "auto" else "auto"
    except Exception:
        src = "auto" if (source or "").lower() == "auto" else (source or "")

    try:
        dur = int(duration_ms)
    except Exception:
        dur = 0
    if dur < 0:
        dur = 0

    try:
        count_targets = len(translations or {})
    except Exception:
        count_targets = 0

    meta: Dict[str, Any] = {
        "duration_ms": dur,
        "targets_count": int(count_targets),
        "provider": provider,
    }

    if extra_meta:
        # defensiv mergen, ohne Exceptions nach außen zu werfen
        for k, v in extra_meta.items():
            try:
                if k not in meta:
                    meta[k] = v
            except Exception:
                continue

    return TranslateResponse(
        source=src,
        translations=translations,
        alternatives=alternatives,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Hilfen für Tests/Adapters
# ---------------------------------------------------------------------------


def asdict_response(resp: TranslateResponse) -> Dict[str, Any]:
    """
    Konvertiert TranslateResponse in ein Dict für jsonify/Tests.

    - translations und meta werden in normale dicts umgewandelt.
    - alternatives (falls vorhanden) wird in Mapping[str, List[str]] übertragen.
    """
    out: Dict[str, Any] = {
        "source": resp.source,
        "translations": dict(resp.translations),
        "meta": dict(resp.meta),
    }
    if resp.alternatives:
        out["alternatives"] = {k: list(v) for k, v in resp.alternatives.items()}
    return out
