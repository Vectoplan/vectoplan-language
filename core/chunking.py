# services/language/core/chunking.py
"""
Intelligentes Chunking für große Texte.

Ziele
- Liefert Chunks <= limit Zeichen.
- Bevorzugt natürliche Grenzen: Absätze → Sätze → Teilsätze → Wörter.
- Fallback auf harte Slices für extrem lange Tokens.
- Keine externen Abhängigkeiten, robust gegen Ausnahmen.
- Optional: HTML-Block-Chunking, ohne Tags zu zerreißen (nur grob, siehe Hinweis).

Hinweis zu HTML:
- `smart_chunks_html()` versucht an Blockgrenzen (<p>, <div>, <h1-6>, <li>, <br>, <section>, <article>, <pre>, <blockquote>)
  zu schneiden. Falls ein einzelner Block > limit ist, wird eine ChunkingError geworfen, statt Tags zu beschädigen.
- Der /translate-Endpoint nutzt standardmäßig **Text-Chunking**. HTML wird dort nicht gechunkt.
"""

from __future__ import annotations

import re
from typing import Generator, Iterable, List, Sequence, Tuple

__all__ = [
    "smart_chunks",
    "smart_chunks_html",
    "chunk_text_smart",
    "chunk_html_blocks",
    "ChunkingError",
]

# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class ChunkingError(Exception):
    """Fehler beim sicheren Chunking (z. B. HTML-Block > limit)."""


# -----------------------------------------------------------------------------
# Text-Splitting-Regeln
# -----------------------------------------------------------------------------

# Absätze: mindestens zwei Zeilenumbrüche als Trenner
_RE_PARA_SPLIT = re.compile(r"(\n{2,})")

# Satzende: ., !, ?, japan./chin. „。！？“, mit evtl. schließenden Anführungen und nachfolgendem Whitespace
_RE_SENT_SPLIT = re.compile(r"([\.!?。！？]+[\"'”’)]*\s+)")

# Teilsatz: Strichpunkt, Doppelpunkt, Komma, Gedankenstrich-Varianten, asiatische Satzzeichen
_RE_CLAUSE_SPLIT = re.compile(r"([;:，、,:；—–\-]\s*)")

# Wörter: Whitespace als Trenner
_RE_WORD_SPLIT = re.compile(r"(\s+)")

# HTML-Blocktags (grobe Heuristik)
_RE_HTML_BLOCK_TAG = re.compile(
    r"(</?(?:p|div|h[1-6]|li|ul|ol|pre|section|article|blockquote|br|hr)[^>]*>)",
    re.IGNORECASE,
)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _split_keep_sep(text: str, pattern: re.Pattern) -> List[str]:
    """
    Splittet nach pattern und behält Trenner auf dem Segment.
    Beispiel: "A. B." → ["A. ", "B."]
    """
    parts = pattern.split(text)
    if len(parts) <= 1:
        return [text]
    out: List[str] = []
    buf = ""
    it = iter(parts)
    for chunk in it:
        sep = next(it, "")
        if sep:
            out.append(chunk + sep)
        else:
            # letzter Rest ohne sep
            if chunk:
                out.append(chunk)
    return [x for x in out if x]


def _pack_units(units: Iterable[str], limit: int) -> List[str]:
    """
    Greedy-Packer: fügt Einheiten zusammen, bis limit erreicht.
    Erwartet: jede Einheit <= limit. Bricht nie Einheiten auf.
    """
    out: List[str] = []
    acc = ""
    for u in units:
        if not u:
            continue
        if len(u) > limit:
            # Programmierfehler: Einheit hätte vorher verfeinert werden müssen
            # Fallback: harte Teilung
            for piece in _hard_slices(u, limit):
                if not acc:
                    acc = piece
                elif len(acc) + len(piece) <= limit:
                    acc += piece
                else:
                    out.append(acc)
                    acc = piece
            continue

        if not acc:
            acc = u
        elif len(acc) + len(u) <= limit:
            acc += u
        else:
            out.append(acc)
            acc = u
    if acc:
        out.append(acc)
    return out


def _hard_slices(text: str, limit: int) -> List[str]:
    """
    Harte Slices ohne Rücksicht auf Grenzen. Letzte Notlösung.
    """
    limit = max(1, int(limit))
    return [text[i : i + limit] for i in range(0, len(text), limit)]


# -----------------------------------------------------------------------------
# Kern: Text-Chunking
# -----------------------------------------------------------------------------

def chunk_text_smart(text: str, limit: int) -> List[str]:
    """
    Chunking-Pipeline für reinen Text:
      Absätze → Sätze → Teilsätze → Wörter → harte Slices.
    Garantiert: Alle Chunks <= limit.
    """
    if not text:
        return [""]
    limit = max(64, int(limit))  # minimale sinnvolle Größe

    if len(text) <= limit:
        return [text]

    # 1) Absätze
    paras = _split_keep_sep(text, _RE_PARA_SPLIT)

    chunks: List[str] = []
    acc = ""

    def _emit(u: str) -> None:
        nonlocal acc, chunks
        if not u:
            return
        if not acc:
            acc = u
        elif len(acc) + len(u) <= limit:
            acc += u
        else:
            chunks.append(acc)
            acc = u

    for p in paras:
        if len(p) <= limit:
            _emit(p)
            continue

        # 2) Sätze
        sents = _split_keep_sep(p, _RE_SENT_SPLIT)
        for s in sents:
            if len(s) <= limit:
                _emit(s)
                continue

            # 3) Teilsätze
            clauses = _split_keep_sep(s, _RE_CLAUSE_SPLIT)
            for c in clauses:
                if len(c) <= limit:
                    _emit(c)
                    continue

                # 4) Wörter
                words = _split_keep_sep(c, _RE_WORD_SPLIT)
                current = ""
                for w in words:
                    if len(w) > limit:
                        # 5) Harte Slices für sehr lange Tokens (z. B. Base64)
                        for piece in _hard_slices(w, limit):
                            if not current:
                                current = piece
                            elif len(current) + len(piece) <= limit:
                                current += piece
                            else:
                                _emit(current)
                                current = piece
                        continue

                    if not current:
                        current = w
                    elif len(current) + len(w) <= limit:
                        current += w
                    else:
                        _emit(current)
                        current = w
                if current:
                    _emit(current)

    if acc:
        chunks.append(acc)

    # Letzte Absicherung
    safe = []
    for ch in chunks:
        if len(ch) <= limit:
            safe.append(ch)
        else:
            safe.extend(_hard_slices(ch, limit))
    return safe


def smart_chunks(text: str, *, limit: int = 10_000) -> List[str]:
    """
    Öffentliche API für Text-Chunking.
    Fällt bei Fehlern robust auf harte Slices zurück.
    """
    try:
        return chunk_text_smart(text, limit)
    except Exception:
        return _hard_slices(text or "", max(1, int(limit)))


# -----------------------------------------------------------------------------
# HTML-Chunking (optional)
# -----------------------------------------------------------------------------

def chunk_html_blocks(html: str, limit: int) -> List[str]:
    """
    Chunking an HTML-Blockgrenzen. Zerreißt Tags nicht absichtlich.
    Wenn ein einzelner Block > limit ist, wird ChunkingError geworfen.
    """
    if not html:
        return [""]

    limit = max(64, int(limit))
    if len(html) <= limit:
        return [html]

    # Tokenisierung: Blöcke + Text
    tokens = _split_keep_sep(html, _RE_HTML_BLOCK_TAG)

    # Prüfe, ob einzelne Token die Grenze schon sprengen
    for tok in tokens:
        if len(tok) > limit:
            # Große <pre>-Blöcke oder lange <p>-Absätze → nicht sicher teilbar
            raise ChunkingError("html_block_exceeds_limit")

    # Packen ohne Tags zu zerreißen
    return _pack_units(tokens, limit)


def smart_chunks_html(html: str, *, limit: int = 10_000) -> List[str]:
    """
    Öffentliche API für HTML-Chunking.
    Gibt bei unsicherer Situation einen Fehler aus, statt HTML zu beschädigen.
    """
    return chunk_html_blocks(html, limit)
