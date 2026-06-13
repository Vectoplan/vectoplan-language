# translate-svc – Language Microservice vor LibreTranslate

Flask-Microservice **translate-svc** als API-Wrapper vor der Open-Source Engine **LibreTranslate**.  
Nimmt Text entgegen, erkennt die Sprache (auto) und liefert Übersetzungen in mehrere Zielsprachen als JSON.  
Der Service ist stateless – keine eigene DB.

Standard-URL in der lokalen Compose-Umgebung:

- Service: `http://localhost:5002`
- Upstream LibreTranslate: intern `http://libretranslate:5000`

---

## Architektur (Überblick)

Komponenten:

- **LibreTranslate**  
  - Container `libretranslate`  
  - Laden/Installieren von Argos-Translate-Paketen über `lt-bootstrap` (Volume `lt_data`).
- **translate-svc** (dieser Dienst)  
  - Python 3.12 + Flask + Gunicorn.
  - App-Fabrik in `services/language/app.py` (`create_app()`).
  - WSGI-Entry in `services/language/wsgi.py` (`wsgi:app`).
  - Startlogik in `services/language/entrypoint.sh`:
    - Wartet optional auf LibreTranslate.
    - Prüft optional Redis.
    - Preload von `wsgi.app`.
    - Startet Gunicorn.
  - Konfiguration über ENV (`config.Settings`).
  - Caching (In-Memory / Redis) über `core.cache`.
  - Rate-Limit (InMemory / Redis) über `core.ratelimit`.
  - Request-Kontext mit Request-ID, Client-IP etc. über `middleware.request_context`.
  - Startup-Selftest über `selftest.py` (optional).

---

## Unterstützte Zielsprachen (18)

Kanonische Codes:

```text
en, de, fr, es, pt, ru, zh-CN, ja, ko, tr, pl, it, nl, ar, id, cs, uk, sv
Hinweise:

Upstream LibreTranslate nutzt für Chinesisch den Engine-Code zh.

Mapping Engine-Code ↔ kanonischer Code passiert intern über core.languages:

to_engine_code("zh-CN") -> "zh"

from_engine_code("zh") -> "zh-CN"

Endpoints
1. POST /translate
Übersetzt einen Text in mehrere Zielsprachen.

Request-Body (JSON)

json
Code kopieren
{
  "text": "Hallo Welt",
  "source": "auto",
  "targets": ["en", "fr", "es"],    // optional; wenn weggelassen -> basierend auf DEFAULT_TARGETS_SOURCE
  "format": "text",                 // "text" | "html"
  "alternatives": 0                 // optional >= 0. Nur wenn Upstream Alternativen unterstützt
}
text: Pflichtfeld. string oder string[] (Liste von Zeilen), wird intern zu einem String mit \n zusammengeführt.

source:

"auto" (Default) → automatische Spracherkennung (einmal pro Request).

oder ISO-Code (z. B. "de", "en", "zh-CN").

targets:

Wenn gesetzt → Liste von Zielsprachen; wird normalisiert und gegen ALLOWED_LANGS und Upstream-Verfügbarkeit geprüft.

Wenn nicht gesetzt → Default-Auswahl basierend auf DEFAULT_TARGETS_SOURCE:

"available": Schnittmenge aus ALLOWED_LANGS ∩ aktuell verfügbaren Upstream-Sprachen.

"allowed": die komplette ALLOWED_LANGS-Liste.

"minimum": READY_MIN_LANGS.

format:

"text" (Default)

"html" → es wird nicht gechunkt; große HTML-Payloads werden mit 413 abgelehnt.

alternatives:

Anzahl zusätzlicher Alternativen (>= 0).

Nur relevant, wenn Upstream dafür Felder liefert.

Response (Beispiel)

json
Code kopieren
{
  "source": "de",                  // erkannte/benutzte Quellsprache
  "translations": {
    "en": "Hello World",
    "fr": "Bonjour le monde",
    "es": "Hola mundo"
  },
  "alternatives": {
    "en": ["Hello, world"]
  },
  "meta": {
    "duration_ms": 123,
    "targets_count": 3,
    "provider": "libretranslate",
    "default_targets_source": "available",
    "available_upstream": ["en", "ar"],
    "chosen_targets_source": "available",
    "cache": {
      "hits": 0,
      "misses": 3
    }
  }
}
source: kanonischer Code oder "auto", abhängig von Detektion/Request.

translations: Mapping zielcode -> Übersetzung.

alternatives: optionales Mapping zielcode -> Liste von Alternativ-Übersetzungen (nur bei alternatives > 0 und falls vorhanden).

meta:

duration_ms: gesamte Dauer des Requests im Microservice.

targets_count: Anzahl Zielsprachen.

provider: aktuell "libretranslate".

default_targets_source: Quelle für die Default-Targets (available | allowed | minimum | fallback_minimum).

available_upstream: Upstream-Sprachen bei Request.

chosen_targets_source: tatsächlich verwendete Quelle.

cache.hits, cache.misses: einfache Cache-Statistik.

2. GET /languages
Liefert gefilterte Upstream-Sprachen, die in ALLOWED_LANGS enthalten sind.

Beispiel-Response

json
Code kopieren
{
  "languages": [
    { "code": "ar", "name": null },
    { "code": "en", "name": null }
  ],
  "count": 2
}
code: kanonischer Sprachcode.

name: derzeit null, kann später mit Anzeigeinformationen gefüllt werden.

3. GET /languages/plan
Zeigt die geplante Sprachen-Installation.

Beispiel:

json
Code kopieren
{
  "allowed": ["en", "de", "..."],
  "install_plan": ["fr", "es", "pt", "..."],
  "count_allowed": 18,
  "count_plan": 16
}
allowed: aus ALLOWED_LANGS normalisiert.

install_plan: aus LT_INSTALL_LANGS normalisiert, auf erlaubte Sprachen gefiltert.

4. GET /languages/status
Status-Endpoint für Sprachen, Upstream und Installationsfortschritt.
Wird von der Web-App genutzt, um in der UI anzuzeigen, welche Sprachen verfügbar sind.

Parameter:

details=1 → zusätzliche Felder (pending).

live=1 → erzwingt einen frischen Upstream-Call; ansonsten kann ein Monitor-Snapshot oder Cache genutzt werden.

Beispiel ohne Details:

json
Code kopieren
{
  "allowed": ["en","de","fr","es","pt","ru","zh-CN","ja","ko","tr","pl","it","nl","ar","id","cs","uk","sv"],
  "install_plan": ["fr","es","pt","ru","zh-CN","ja","ko","tr","pl","it","nl","ar","id","cs","uk","sv"],
  "available": ["en","ar"],
  "installing": [],
  "pending_count": 15,
  "progress_pct": 6,
  "service": {
    "name": "translate-svc",
    "version": "0.1.0",
    "default_targets_source": "available",
    "wait_for_upstream": false
  },
  "upstream": {
    "base_url": "http://libretranslate:5000",
    "status": 200,
    "latency_ms": 3,
    "reachable": true,
    "error": null,
    "last_check_ts": 1760725937717
  },
  "fs_probe": {
    "enabled": true,
    "mount": "/ltdata",
    "detected_count": 4,
    "staged_langs": ["sq","az","eu","bn"]
  }
}
Mit details=1&live=1 zusätzlich:

json
Code kopieren
{
  "pending": ["cs","es","fr","id","it","ja","ko","nl","pl","pt","ru","sv","tr","uk","zh-CN"],
  ...
}
5. Health / Readiness
GET /_health
Liefert 200, wenn der Prozess läuft (unabhängig vom Upstream).

Minimal-JSON:

json
Code kopieren
{
  "status": "ok",
  "service": "translate-svc",
  "version": "0.1.0"
}
GET /_ready
Readiness-Endpoint, der auch den Upstream berücksichtigt:

200 (ready: true) wenn:

LibreTranslate erreichbar ist (/languages → 2xx),

und mindestens READY_MIN_LANGS im Upstream verfügbar sind.

503 (ready: false) sonst.

Beispiel bei Erfolg:

json
Code kopieren
{
  "ready": true,
  "wait_for_upstream": true,
  "live": false,
  "upstream": {
    "source": "monitor",     // oder "direct"
    "reachable": true,
    "status": 200,
    "latency_ms": 3,
    "langs_available": ["en","ar"],
    "missing_required": [],
    "required_min": ["en","de"],
    "error": null
  },
  "service": {
    "name": "translate-svc",
    "version": "0.1.0",
    "languages_allowed": ["en","de", "..."]
  }
}
Query:

live=1 → erzwingt Live-Check gegen LibreTranslate.

Fehler (HTTP/JSON)
Typische Fehlercodes aus dem Microservice:

400 invalid_argument

z. B. ungültiger Payload (invalid_json, text_empty, text_too_large, targets_must_be_list, no_valid_targets, unsupported_format, too_many_targets, targets_not_available).

413 payload_too_large

Text > MAX_CHARS, insbesondere bei HTML.

415 unsupported_media_type

Kein application/json.

429 rate_limited

Rate-Limit pro Client/IP überschritten (Token-Bucket).

502 upstream_error

Upstream gibt unerwarteten Status (z. B. 5xx) zurück.

504 timeout

Upstream-/Request-Timeout überschritten.

503 not_ready

Upstream nicht bereit (z. B. WAIT_FOR_UPSTREAM=1 und READY_MIN_LANGS nicht verfügbar).

599 network_error

Synthetischer Netzfehler (DNS, Verbindung, etc.).

Alle Fehlerantworten folgen der Struktur:

json
Code kopieren
{
  "error": {
    "code": "invalid_argument",
    "message": "text_too_large",
    "details": { "max_chars": 10000, "got": 12000 }
  },
  "meta": {
    "request_id": "..."
  }
}
Header
X-Request-ID: wird für alle Antworten gesetzt (entweder aus Eingang oder generiert).

X-Process-Time-ms: Dauer der internen Verarbeitung (Millisekunden).

Rate-Limit:

X-RateLimit-Remaining: verbleibende Tokens.

Retry-After: bei 429 oder 503 gesetzt, wenn sinnvoll.

Selftest / Smoke-Test
Beim Start des Containers kann ein Selftest aktiviert werden:

Selftest läuft im Hintergrund und prüft:

/_ready → Upstream + Sprachen.

POST /translate mit Beispieltext (SELFTEST_SAMPLE_TEXT).

Konfiguration (ENV):

SELFTEST_ENABLED (Default: 1)

SELFTEST_BASE_URL (Default: http://127.0.0.1:${PORT})

SELFTEST_READY_PATH (Default: /_ready)

SELFTEST_TRANSLATE_PATH (Default: /translate)

SELFTEST_MAX_WAIT_SEC (Default: 60)

SELFTEST_RETRY_INTERVAL_SEC (Default: 2)

SELFTEST_TIMEOUT_SEC (Default: 5)

SELFTEST_SAMPLE_TEXT (Default: deutscher Beispieltext)

Log-Beispiele:

Erfolgreich:

selftest_ready_ok

selftest_translate_ok

Fehler:

selftest_ready_timeout

selftest_translate_failed

Manuelle Tests
PowerShell (Windows) – alle 18 Sprachen
powershell
Code kopieren
$body = @{
  text   = "Hallo zusammen, mir geht es heute gut und wie geht es dir?"
  source = "auto"
} | ConvertTo-Json

Invoke-RestMethod -Uri http://localhost:5002/translate -Method POST -ContentType 'application/json' -Body $body |
  ConvertTo-Json -Depth 6
curl (Linux/macOS/WSL)
bash
Code kopieren
curl -X POST http://localhost:5002/translate \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hallo zusammen, mir geht es heute gut und wie geht es dir?",
    "source": "auto"
  }' | jq
Konfiguration (ENV – Auszug)
Name	Default	Beschreibung
PORT	8000	Listen-Port des Microservice
HOST	0.0.0.0	Bind-Adresse
LOG_LEVEL	INFO	Log-Level (debug, info, warning, error, critical)
LT_BASE_URL	http://libretranslate:5000	Upstream-URL von LibreTranslate
LT_API_KEY	–	Optionaler API-Key (wird als api_key im JSON an Upstream übergeben)
ALLOWED_LANGS	18er-Liste	Whitelist kanonischer Zielsprachen
READY_MIN_LANGS	en,de	Minimale Menge, damit /_ready bei WAIT_FOR_UPSTREAM=1 „ready“ wird
DEFAULT_TARGETS_SOURCE	available	available | allowed | minimum
WAIT_FOR_UPSTREAM	0	1 → /_ready wird erst true, wenn Upstream + READY_MIN_LANGS verfügbar sind
READY_PROBE_INTERVAL_SEC	30	Interval für Upstream-Probe/Cache im Readiness-Check
UPSTREAM_TIMEOUT_SEC	10	Timeout für Upstream-Abfragen im Readiness-/Status-Endpoint
MAX_CHARS	10000	Maximale Textlänge pro Request
MAX_TARGETS	10	Limit, wenn der Client targets explizit setzt
CACHE_ENABLED	0	Cache an/aus
CACHE_TTL_SEC	3600	TTL für Cache-Einträge
CACHE_MAX_ITEMS	10000	Max. Items im InMemory-Cache
CACHE_VERSION	1	Version für Cache-Key (z. B. bei Modellwechsel erhöhen)
REDIS_URL	–	z. B. redis://redis:6379/0 – wenn gesetzt, nutzt Cache/Rate-Limit Redis
RATE_LIMIT_ENABLED	0	Rate-Limit an/aus
RATE_BUCKET	60	Bucket-Kapazität (Tokens)
RATE_REFILL	1	Refill-Rate (Tokens/Sekunde)
CORS_ORIGINS	–	Komma/; getrennte Liste erlaubter Origins (z. B. http://localhost:5000)
SERVICE_NAME	translate-svc	Name des Dienstes (für Logs/Status)
VERSION / APP_VERSION	aus Build-Arg BUILD_VERSION	Versionskennung im Status
SELFTEST_*	siehe Abschnitt „Selftest / Smoke-Test“	Steuerung des Startup-Selftests

Logging
JSON-Logging über core.logging:

Felder u. a.:

ts (ISO-8601 UTC)

level

logger

msg

request_id (falls vorhanden)

Kontext (client_ip, client_id, method, path, host)

Gunicorn-Logs (Access/Error) werden an Root-Logger propagiert.

Beispiel-Logzeilen:

logging_initialized

app_created

translate_start

translate_unhandled_exception

selftest_translate_ok / selftest_translate_failed

Docker / Compose (Kurz)
lt-bootstrap:

Installiert Argos-Modelle in lt_data Volume.

libretranslate:

Läuft mit XDG_DATA_HOME=/data/share, Volume lt_data:/data.

translate-svc:

Baut aus ./services/language.

Umgebungsvariablen wie oben.

REDIS_URL=redis://redis:6379/0.

Healthcheck → /_ready.

Extern: 5002:8000 gemappt.

Die Web-App (flask-app) nutzt:

TR_SVC_BASE_URL=http://translate-svc:8000 für Übersetzungen.

LANGUAGE_API_BASE=http://translate-svc:8000 für /languages/status.