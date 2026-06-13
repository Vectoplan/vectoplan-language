# services/language/clients/libretranslate.py
"""
LibreTranslate-Client mit korrektem Sprachcode-Mapping und robustem JSON-Handling.

- /languages akzeptiert Top-Level-Listen ODER {"languages":[...]} / {"data":[...]}.
- detect() und translate() sind fehlertolerant (Timeouts, unerwartete Felder).
- Engine-Codes ↔ kanonische Codes via core.languages.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .http import HttpClient, HttpClientConfig, HttpError
from core.languages import canon_lang as _canon, to_engine_code, from_engine_code

JSONType = Union[Dict[str, Any], List[Any]]

# -----------------------------------------------------------------------------#
# Typen
# -----------------------------------------------------------------------------#


@dataclass
class LanguageInfo:
    code: str
    name: Optional[str] = None


@dataclass
class Detection:
    language: str
    confidence: float


@dataclass
class TranslationResult:
    source: str          # kanonisch
    target: str          # kanonisch
    text: str
    translated: str
    alternatives: Optional[List[str]] = None
    duration_ms: int = 0
    raw: Optional[Mapping[str, object]] = None
    provider: str = "libretranslate"


# -----------------------------------------------------------------------------#
# Helpers
# -----------------------------------------------------------------------------#


def _canon_targets(targets: Iterable[str]) -> Tuple[str, ...]:
    seen = set()
    out: List[str] = []
    for t in targets:
        ct = _canon(t)
        if not ct or ct in seen:
            continue
        seen.add(ct)
        out.append(ct)
    return tuple(out)


def _pick_list(container: JSONType) -> List[Any]:
    """
    Nimmt JSON von /languages und extrahiert eine Liste von Items.
    Akzeptiert Top-Level-Liste oder Mapping mit Key 'languages'/'data'/'langs'.
    """
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        for key in ("languages", "data", "langs"):
            v = container.get(key)
            if isinstance(v, list):
                return v
    return []


def _as_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


# -----------------------------------------------------------------------------#
# Client
# -----------------------------------------------------------------------------#


class LibreTranslateClient:
    def __init__(
        self,
        http_config: HttpClientConfig,
        *,
        api_key: Optional[str] = None,
        allowed_langs: Optional[Sequence[str]] = None,
    ) -> None:
        self.http = HttpClient(http_config)
        self.api_key = api_key
        self.allowed_langs: Tuple[str, ...] = tuple(_canon(x) for x in (allowed_langs or ()))
        self._log = logging.getLogger(__name__)

    def close(self) -> None:
        self.http.close()

    # ---------------------------- API ----------------------------

    def languages(self) -> List[LanguageInfo]:
        """
        Liest die verfügbaren Sprachen vom Upstream und mappt sie auf kanonische Codes.
        """
        data, status, _ = self.http.get_json("/languages", expected=(200,))
        out: List[LanguageInfo] = []

        try:
            items = _pick_list(data) if isinstance(data, (dict, list)) else []
            for item in items:
                try:
                    if isinstance(item, dict):
                        eng = str(item.get("code") or item.get("lang") or item.get("id") or "").strip()
                        name = (str(item.get("name") or "").strip() or None)
                    elif isinstance(item, str):
                        eng = item.strip()
                        name = None
                    else:
                        continue
                    if not eng:
                        continue
                    can = from_engine_code(eng, engine="libretranslate")
                    if can:
                        out.append(LanguageInfo(code=can, name=name))
                except Exception:
                    # einzelnes Item ignorieren
                    continue
        except Exception as exc:
            self._log.warning("languages_parse_error status=%s err=%s", status, type(exc).__name__)
            out = []

        if self.allowed_langs:
            allow = set(self.allowed_langs)
            out = [x for x in out if x.code in allow]

        # Deduplizieren, stabile Reihenfolge
        seen = set()
        dedup: List[LanguageInfo] = []
        for li in out:
            if li.code in seen:
                continue
            seen.add(li.code)
            dedup.append(li)
        return dedup

    def detect(self, text: str, *, top_n: int = 1, timeout_sec: Optional[int] = None) -> List[Detection]:
        """
        Spracherkennung. Akzeptiert mögliche Varianten der LT-Antwort.
        """
        if not text:
            return []
        payload: Dict[str, object] = {"q": text}
        if self.api_key:
            payload["api_key"] = self.api_key

        data, status, _ = self.http.post_json("/detect", json_body=payload, expected=(200,), timeout_sec=timeout_sec)
        out: List[Detection] = []
        try:
            items: List[Any]
            if isinstance(data, list) and data and isinstance(data[0], list):
                # manche Implementierungen liefern [[{language, confidence}]]
                items = data[0]
            elif isinstance(data, list):
                items = data
            elif isinstance(data, dict) and isinstance(data.get("detections"), list):
                items = data.get("detections")  # type: ignore
            else:
                items = []

            for item in items:
                try:
                    if not isinstance(item, dict):
                        continue
                    eng = str(item.get("language", "")).strip()
                    can = from_engine_code(eng, engine="libretranslate")
                    conf = _as_float(item.get("confidence", 0.0), 0.0)
                    if can:
                        out.append(Detection(language=can, confidence=conf))
                except Exception:
                    continue
        except Exception as exc:
            self._log.warning("detect_parse_error status=%s err=%s", status, type(exc).__name__)

        out.sort(key=lambda d: d.confidence, reverse=True)
        return out[:top_n] if top_n > 0 else out

    def translate(
        self,
        text: str,
        *,
        source: str = "auto",
        target: str,
        format: str = "text",
        alternatives: int = 0,
        timeout_sec: Optional[int] = None,
        allow_detect_fallback: bool = True,
    ) -> TranslationResult:
        """
        Einzeln-Zielübersetzung. Fällt robust zurück, wenn Felder fehlen.
        """
        if not text:
            return TranslationResult(
                source=_canon(source) if source else "auto",
                target=_canon(target),
                text="",
                translated="",
                alternatives=None,
                duration_ms=0,
                raw=None,
            )

        source_can = _canon(source) if source else "auto"
        target_can = _canon(target)

        if self.allowed_langs and target_can not in self.allowed_langs:
            raise HttpError(400, "target_not_allowed", code="invalid_argument", details={"target": target_can})

        used_source_can = source_can
        if source_can == "auto" and allow_detect_fallback:
            try:
                det = self.detect(text, top_n=1, timeout_sec=timeout_sec)
                if det:
                    used_source_can = det[0].language
            except HttpError:
                used_source_can = "auto"

        # Map nach Engine-Codes
        src_engine = (
            used_source_can
            if used_source_can == "auto"
            else to_engine_code(used_source_can, engine="libretranslate")
        )
        tgt_engine = to_engine_code(target_can, engine="libretranslate")

        started = time.perf_counter()
        payload: Dict[str, object] = {
            "q": text,
            "source": src_engine,
            "target": tgt_engine,
            "format": "html" if format == "html" else "text",
        }
        if alternatives and alternatives > 0:
            payload["alternatives"] = int(alternatives)
        if self.api_key:
            payload["api_key"] = self.api_key

        data, status, _ = self.http.post_json("/translate", json_body=payload, expected=(200,), timeout_sec=timeout_sec)

        translated_text = ""
        alts: Optional[List[str]] = None
        try:
            if isinstance(data, dict):
                # Standard LT
                translated_text = str(data.get("translatedText", "")).strip()
                if "alternatives" in data and isinstance(data["alternatives"], list):
                    alts = [str(x) for x in data["alternatives"]]
            elif isinstance(data, list) and data:
                # Einige Wrapper liefern [{"translatedText": "..."}]
                first = data[0]
                if isinstance(first, dict):
                    translated_text = str(first.get("translatedText", "")).strip()
        except Exception:
            translated_text = ""

        dur_ms = int((time.perf_counter() - started) * 1000)

        return TranslationResult(
            source=used_source_can,
            target=target_can,
            text=text,
            translated=translated_text,
            alternatives=alts,
            duration_ms=dur_ms,
            raw=data if isinstance(data, dict) else None,
        )

    def translate_many_targets(
        self,
        text: str,
        *,
        source: str = "auto",
        targets: Sequence[str],
        format: str = "text",
        alternatives: int = 0,
        timeout_sec: Optional[int] = None,
    ) -> Dict[str, TranslationResult]:
        """
        Übersetzt einen Text in mehrere Zielsprachen.

        - Normalisiert Targets.
        - Nutzt eine einmalige Spracherkennung, wenn source="auto".
        - Ruft translate(...) pro Ziel auf und aggregiert die Ergebnisse.
        """
        tgt = _canon_targets(targets)
        if not tgt:
            raise HttpError(400, "missing_targets", code="invalid_argument")
        results: Dict[str, TranslationResult] = {}

        used_source_can = _canon(source) if source else "auto"
        pre_detected: Optional[str] = None
        if used_source_can == "auto":
            try:
                det = self.detect(text, top_n=1, timeout_sec=timeout_sec)
                if det:
                    pre_detected = det[0].language
            except HttpError:
                pre_detected = None

        for t in tgt:
            res = self.translate(
                text,
                source=pre_detected or used_source_can,
                target=t,
                format=format,
                alternatives=alternatives,
                timeout_sec=timeout_sec,
                allow_detect_fallback=False,
            )
            results[t] = res
        return results


# -----------------------------------------------------------------------------#
# Builder
# -----------------------------------------------------------------------------#


def build_client_from_config(
    *,
    base_url: str,
    timeout_sec: int = 30,
    api_key: Optional[str] = None,
    allowed_langs: Optional[Sequence[str]] = None,
) -> LibreTranslateClient:
    http_cfg = HttpClientConfig(
        base_url=base_url,
        timeout_sec=timeout_sec,
        default_headers={"Accept": "application/json"},
        api_key_header=None,
        api_key_value=None,
        verify_tls=base_url.startswith("https://"),
        retries=3,
        backoff_base=0.2,
        backoff_max=3.0,
    )
    return LibreTranslateClient(http_cfg, api_key=api_key, allowed_langs=allowed_langs)
