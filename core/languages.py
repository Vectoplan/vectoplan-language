# services/language/core/languages.py
"""
Zentrale Sprachlogik für translate-svc.

Ziele
------
- Eine Quelle für Sprachcode-Normalisierung und Aliase.
- Kanonische Codes für UI/DB, Engine-Codes für Upstream (LibreTranslate).
- Anzeigeinformationen (englischer Name, Native-Name), RTL-Flag.
- Hilfsfunktionen zum Filtern/Validieren und für Zielsprachen-Listen.

Begriffe
--------
- *kanonisch*: BCP-47-ähnlich, kleingeschriebene Basis, optionale Region groß.
  Beispiele: "en", "de", "pt", "zh-CN", "zh-TW".
- *engine*: Code, den der Upstream erwartet. LibreTranslate nutzt z. T. Basiscodes,
  z. B. "zh" statt "zh-CN/zh-TW".

Invarianten
-----------
- canon_lang("ZH_hans") -> "zh-CN"
- canon_lang("pt-BR")   -> "pt"
- to_engine_code("zh-CN") -> "zh"
- from_engine_code("zh")  -> "zh-CN"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "DEFAULT_ALLOWED_LANGS",
    "KNOWN_LANGS",
    "canon_lang",
    "canon_targets",
    "to_engine_code",
    "from_engine_code",
    "is_rtl",
    "info_for",
    "list_infos",
    "filter_allowed",
    "load_allowed_from_config",
    "normalize_or_default",
]

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults und bekannte Sprachen
# ---------------------------------------------------------------------------

# Standard-Whitelist für den Service (18 Sprachen).
DEFAULT_ALLOWED_LANGS: Tuple[str, ...] = (
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

# Bekannte kanonische Codes (Whitelist für Konfiguration).
# Hier können später gefahrlos weitere Codes ergänzt werden.
KNOWN_LANGS: Tuple[str, ...] = DEFAULT_ALLOWED_LANGS + (
    "zh-TW",  # optionale Erweiterung
    "he",
    "fa",
    "ur",
)

# Aliase für Eingangscodes → kanonische Codes.
# Alle Keys müssen lower-case sein.
_ALIAS: Dict[str, str] = {
    # Region-/Varietäten zu Basis
    "es-es": "es",
    "es-mx": "es",
    "pt-pt": "pt",
    "pt-br": "pt",
    "en-us": "en",
    "en-gb": "en",
    "en-au": "en",
    "de-de": "de",
    "fr-fr": "fr",
    "it-it": "it",
    "nl-nl": "nl",
    "pl-pl": "pl",
    "tr-tr": "tr",
    "sv-se": "sv",
    "cs-cz": "cs",
    "uk-ua": "uk",
    "ru-ru": "ru",
    # Chinesisch-Varianten
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
    "zh-hk": "zh-TW",
    # Legacy/Historische
    "iw": "he",  # veraltet für he
    # Norwegisch-Varianten optional (nicht in KNOWN_LANGS Whitelist enthalten)
    "no": "no",
    "nb": "no",
}

# Engine-Code-Mapping (LibreTranslate): Kanonisch -> Engine
_ENGINE_MAP_LIBRE: Dict[str, str] = {
    "zh-CN": "zh",
    "zh-TW": "zh",  # LibreTranslate unterscheidet meist nicht
}

# ---------------------------------------------------------------------------
# Anzeigeinformationen
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LangInfo:
    """Metadaten zu einer Sprache für UI/Anzeigezwecke."""

    code: str
    name: str
    native: str
    rtl: bool


# Englische Namen + Native-Namen.
# Hinweis: Diese Liste ist für UI gedacht – für Logik ist nur `code` relevant.
_LANG_INFOS: Dict[str, LangInfo] = {
    "en": LangInfo("en", "English", "English", False),
    "de": LangInfo("de", "German", "Deutsch", False),
    "fr": LangInfo("fr", "French", "Français", False),
    "es": LangInfo("es", "Spanish", "Español", False),
    "pt": LangInfo("pt", "Portuguese", "Português", False),
    "ru": LangInfo("ru", "Russian", "Русский", False),
    "zh-CN": LangInfo("zh-CN", "Chinese (Simplified)", "简体中文", False),
    "zh-TW": LangInfo("zh-TW", "Chinese (Traditional)", "繁體中文", False),
    "ja": LangInfo("ja", "Japanese", "日本語", False),
    "ko": LangInfo("ko", "Korean", "한국어", False),
    "tr": LangInfo("tr", "Turkish", "Türkçe", False),
    "pl": LangInfo("pl", "Polish", "Polski", False),
    "it": LangInfo("it", "Italian", "Italiano", False),
    "nl": LangInfo("nl", "Dutch", "Nederlands", False),
    "ar": LangInfo("ar", "Arabic", "العربية", True),
    "id": LangInfo("id", "Indonesian", "Bahasa Indonesia", False),
    "cs": LangInfo("cs", "Czech", "Čeština", False),
    "uk": LangInfo("uk", "Ukrainian", "Українська", False),
    "sv": LangInfo("sv", "Swedish", "Svenska", False),
    # optionale künftige RTLs
    "he": LangInfo("he", "Hebrew", "עברית", True),
    "fa": LangInfo("fa", "Persian", "فارسی", True),
    "ur": LangInfo("ur", "Urdu", "اردو", True),
}

# ---------------------------------------------------------------------------
# interne Helfer
# ---------------------------------------------------------------------------


def _safe_str(value: Any) -> str:
    """Konvertiert robust zu str, ohne Exceptions nach außen zu werfen."""
    try:
        if value is None:
            return ""
        return str(value)
    except Exception:  # pragma: no cover - reine Sicherheitsmaßnahme
        try:
            return repr(value)
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# Normalisierung
# ---------------------------------------------------------------------------


def canon_lang(code: str) -> str:
    """
    Normalisiert Sprachcodes defensiv.

    Regeln (vereinfacht)
    --------------------
    - trims, '_' → '-'
    - untere Schreibweise der Basis
    - Aliase/Regionen laut _ALIAS werden aufgelöst
    - zh/zh-CN/zh-Hans -> 'zh-CN'; zh-TW/zh-Hant/zh-HK -> 'zh-TW'
    - regionale Varianten außer zh-* auf Basissprache reduzieren (pt-BR -> pt)
    """
    try:
        c = _safe_str(code).strip().replace("_", "-")
        if not c:
            return c

        low = c.lower()

        # Aliase zuerst auflösen
        aliased = _ALIAS.get(low)
        if aliased is not None:
            low = aliased

        # Region vereinheitlichen (z. B. pt-BR -> pt, zh-tw -> zh-TW)
        if len(low) == 5 and low[2] == "-":
            base, region = low.split("-", 1)
            if base == "zh":
                # zh-xx -> zh-XX
                return f"{base}-{region.upper()}"
            # alle Nicht-zh-Varianten laufen auf Basissprache ein
            return base

        # Rohes "zh" sicher abbilden
        if low == "zh":
            return "zh-CN"

        return low
    except Exception:  # pragma: no cover - defensive
        log.warning("canon_lang_failed", exc_info=True)
        return ""


def canon_targets(items: Iterable[str]) -> Tuple[str, ...]:
    """
    Normalisiert eine beliebige Iterable von Sprachcodes.

    - nutzt canon_lang()
    - verwirft leere/ungültige Codes
    - dedupliziert, Reihenfolge der ersten Vorkommen bleibt erhalten
    """
    seen = set()
    out: List[str] = []
    try:
        for raw in items or []:
            cx = canon_lang(raw)
            if not cx:
                continue
            if cx in seen:
                continue
            seen.add(cx)
            out.append(cx)
    except Exception:  # pragma: no cover - defensive
        log.warning("canon_targets_failed", exc_info=True)
    return tuple(out)


# ---------------------------------------------------------------------------
# Engine-Mapping
# ---------------------------------------------------------------------------


def to_engine_code(code: str, *, engine: str = "libretranslate") -> str:
    """
    Mappt kanonischen Code → Engine-Code.

    LibreTranslate:
    - zh-CN/zh-TW -> zh
    - sonst unverändert
    """
    c = canon_lang(code)
    try:
        if engine == "libretranslate":
            return _ENGINE_MAP_LIBRE.get(c, c)
        return c
    except Exception:  # pragma: no cover - defensive
        log.warning("to_engine_code_failed", exc_info=True)
        return c


def from_engine_code(code: str, *, engine: str = "libretranslate") -> str:
    """
    Mappt Engine-Code → kanonischer Code.

    LibreTranslate:
    - zh -> zh-CN (Default: vereinfachtes Chinesisch)
    - alle anderen Codes über canon_lang()
    """
    low = _safe_str(code).strip().lower()
    try:
        if engine == "libretranslate":
            if low == "zh":
                return "zh-CN"
            return canon_lang(low)
        return canon_lang(low)
    except Exception:  # pragma: no cover - defensive
        log.warning("from_engine_code_failed", exc_info=True)
        return canon_lang(low)


# ---------------------------------------------------------------------------
# Anzeige + RTL
# ---------------------------------------------------------------------------


def is_rtl(code: str) -> bool:
    """Gibt True zurück, wenn die Sprache rechts-nach-links geschrieben wird."""
    try:
        c = canon_lang(code)
        info = _LANG_INFOS.get(c)
        return bool(info and info.rtl)
    except Exception:  # pragma: no cover - defensive
        log.warning("is_rtl_failed", exc_info=True)
        return False


def info_for(code: str) -> LangInfo:
    """
    Liefert Anzeigeinformationen für einen Code.

    Fallback: generiertes LangInfo-Objekt mit name/native == code, rtl=False.
    """
    try:
        c = canon_lang(code)
        info = _LANG_INFOS.get(c)
        if info is not None:
            return info
        return LangInfo(c, c, c, False)
    except Exception:  # pragma: no cover - defensive
        log.warning("info_for_failed", exc_info=True)
        c = _safe_str(code) or "unknown"
        return LangInfo(c, c, c, False)


def list_infos(codes: Sequence[str]) -> List[LangInfo]:
    """
    Liefert LangInfo-Liste für eine Codes-Sequence.

    - nutzt canon_targets() → dedupliziert & normalisiert
    - Unknowns werden als generische LangInfo zurückgegeben
    """
    out: List[LangInfo] = []
    try:
        for c in canon_targets(codes):
            out.append(info_for(c))
    except Exception:  # pragma: no cover - defensive
        log.warning("list_infos_failed", exc_info=True)
    return out


# ---------------------------------------------------------------------------
# Filter/Validierung
# ---------------------------------------------------------------------------


def filter_allowed(codes: Sequence[str], allowed: Sequence[str]) -> Tuple[str, ...]:
    """
    Filtert eine Codesliste gegen eine erlaubte Sprachenliste.

    - Beide Listen werden kanonisiert.
    - Unbekannte/ungültige Codes werden verworfen.
    """
    try:
        allow = {canon_lang(a) for a in (allowed or []) if canon_lang(a)}
        if not allow:
            # Wenn allowed leer/ungültig: alles wegfiltern
            return tuple()
        return tuple([c for c in canon_targets(codes) if c in allow])
    except Exception:  # pragma: no cover - defensive
        log.warning("filter_allowed_failed", exc_info=True)
        return tuple()


def normalize_or_default(
    codes: Optional[Sequence[str]],
    *,
    allowed: Sequence[str],
    default_to_allowed: bool = True,
) -> Tuple[str, ...]:
    """
    Normalisiert Zielsprachenliste.

    - Wenn `codes` gesetzt: filter_allowed(codes, allowed)
    - Wenn `codes` None/leer und default_to_allowed=True:
      komplette normalisierte allowed-Liste.
    - Entfernt Unbekannte, dedupliziert.
    """
    try:
        if codes:
            return filter_allowed(codes, allowed)
        if default_to_allowed:
            return canon_targets(allowed)
        return tuple()
    except Exception:  # pragma: no cover - defensive
        log.warning("normalize_or_default_failed", exc_info=True)
        # konservativer Fallback
        return canon_targets(allowed) if default_to_allowed else tuple()


# ---------------------------------------------------------------------------
# Konfiguration / Laden aus App-Config
# ---------------------------------------------------------------------------


def _parse_langs_from_any(raw: Any) -> List[str]:
    """
    Hilfsfunktion: extrahiert eine Liste von Sprachcodes aus verschiedenen Typen.

    Unterstützt:
    - None -> []
    - str  -> Split an , und ;  (z. B. "en,de,fr")
    - Sequence -> einzelne Elemente zu str() konvertieren
    - sonst -> []
    """
    try:
        if raw is None:
            return []
        if isinstance(raw, str):
            items = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
            return items
        if isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
            out: List[str] = []
            for x in raw:
                s = _safe_str(x).strip()
                if s:
                    out.append(s)
            return out
    except Exception:  # pragma: no cover - defensive
        log.warning("parse_langs_from_any_failed", exc_info=True)
    return []


def load_allowed_from_config(config: Mapping[str, object]) -> Tuple[str, ...]:
    """
    Liest ALLOWED_LANGS aus App-Config, normalisiert auf kanonische Codes,
    verwirft Unbekannte, fällt auf DEFAULT_ALLOWED_LANGS zurück.

    Erwartet:
      - config["ALLOWED_LANGS"] kann sein:
          * None → DEFAULT_ALLOWED_LANGS
          * str  → "en,de,fr"
          * Sequence[str] → Liste/Tuple
          * alles andere wird ignoriert
    """
    try:
        raw = None
        try:
            raw = config.get("ALLOWED_LANGS", None)  # type: ignore[assignment]
        except Exception:
            # Mapping könnte "komisch" sein – wir loggen und nutzen Default
            log.warning("config_get_ALLOWED_LANGS_failed", exc_info=True)

        items = _parse_langs_from_any(raw)
        if not items:
            items = list(DEFAULT_ALLOWED_LANGS)

        norm = canon_targets(items)
        # Unbekannte verwerfen
        filtered = tuple([c for c in norm if c in KNOWN_LANGS])
        if filtered:
            return filtered

        # Wenn nach Filter nichts übrig bleibt → harte Defaults
        log.warning(
            "load_allowed_from_config_no_valid_langs_fallback_default",
            extra={"raw": raw},
        )
        return DEFAULT_ALLOWED_LANGS
    except Exception:  # pragma: no cover - defensive
        log.warning("load_allowed_from_config_failed_fallback_default", exc_info=True)
        return DEFAULT_ALLOWED_LANGS
