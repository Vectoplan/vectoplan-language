# services/language/entrypoint.sh
#!/usr/bin/env bash
# Start-Skript für translate-svc
# - Setzt sichere Shell-Optionen
# - Optionales Warten auf LibreTranslate-Upstream
# - Optionale Checks für Redis
# - Startet Gunicorn mit unserer WSGI-App
set -Eeuo pipefail

# --------------------------- Logging ---------------------------
iso_ts() { date -u +"%Y-%m-%dT%H:%M:%S.%3NZ"; }
log()     { printf '%s %-5s %s\n' "$(iso_ts)" "INFO"  "$*" >&1; }
warn()    { printf '%s %-5s %s\n' "$(iso_ts)" "WARN"  "$*" >&2; }
err()     { printf '%s %-5s %s\n' "$(iso_ts)" "ERROR" "$*" >&2; }

mask() {
  # verdeckt Geheimnisse in Logs
  local v="${1:-}"
  if [ -z "${v:-}" ]; then echo ""; return; fi
  if [ "${#v}" -le 6 ]; then echo "******"; return; fi
  echo "${v:0:2}****${v: -2}"
}

# --------------------------- Defaults ---------------------------
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"
export TZ="${TZ:-Europe/Berlin}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

LT_BASE_URL="${LT_BASE_URL:-http://libretranslate:5000}"
LT_API_KEY="${LT_API_KEY:-}"

ALLOWED_LANGS="${ALLOWED_LANGS:-en,de,fr,es,pt,ru,zh-CN,ja,ko,tr,pl,it,nl,ar,id,cs,uk,sv}"

HTTP_TIMEOUT_SEC="${HTTP_TIMEOUT_SEC:-30}"
WAIT_FOR_UPSTREAM="${WAIT_FOR_UPSTREAM:-1}"
WAIT_UPSTREAM_URL="${WAIT_UPSTREAM_URL:-$LT_BASE_URL/languages}"
WAIT_UPSTREAM_TIMEOUT_SEC="${WAIT_UPSTREAM_TIMEOUT_SEC:-180}"
WAIT_UPSTREAM_INTERVAL_SEC="${WAIT_UPSTREAM_INTERVAL_SEC:-2}"

CACHE_ENABLED="${CACHE_ENABLED:-0}"
CACHE_TTL_SEC="${CACHE_TTL_SEC:-3600}"
REDIS_URL="${REDIS_URL:-}"

RATE_LIMIT_ENABLED="${RATE_LIMIT_ENABLED:-0}"
RATE_BUCKET="${RATE_BUCKET:-60}"
RATE_REFILL="${RATE_REFILL:-1}"

SERVICE_NAME="${SERVICE_NAME:-translate-svc}"
APP_VERSION="${APP_VERSION:-0.1.0}"

EXTRA_GUNICORN_ARGS="${EXTRA_GUNICORN_ARGS:-}"

# --------------------------- Traps ---------------------------
child_pid=""
on_term() {
  warn "termination_signal_received; forwarding to child pid=${child_pid:-unset}"
  if [ -n "${child_pid:-}" ] && kill -0 "${child_pid}" 2>/dev/null; then
    kill -TERM "${child_pid}" || true
    wait "${child_pid}" || true
  fi
  exit 143
}
trap on_term TERM INT

# --------------------------- Sanity checks ---------------------------
need_bin() {
  if ! command -v "$1" >/dev/null 2>&1; then
    err "missing binary: $1"
    exit 127
  fi
}
need_bin python
need_bin gunicorn
# curl optional, aber bevorzugt
if ! command -v curl >/dev/null 2>&1; then
  warn "curl not found; waiting for upstream will use python+urllib"
fi

# --------------------------- Helpers ---------------------------
http_get_json_status() {
  # Liefert HTTP-Status für GET $1
  local url="$1"
  local timeout="${2:-10}"

  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null -m "${timeout}" -w "%{http_code}" \
      -H "Accept: application/json" \
      "${url}" || true
    return
  fi

  # Fallback via Python stdlib
  python - "$url" "$timeout" <<'PY'
import json, sys, urllib.request
url = sys.argv[1]
timeout = int(sys.argv[2])
try:
    req = urllib.request.Request(url, headers={"Accept":"application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        status = getattr(r, "status", 200) or 200
        print(status)
except Exception:
    # synthetischer Fehlerstatus
    print("599")
PY
}

wait_for_upstream() {
  local url="$1"
  local timeout_total="$2"
  local interval="$3"

  log "waiting for upstream: url=${url} timeout=${timeout_total}s interval=${interval}s"
  local start_ts
  start_ts="$(date +%s)"
  local now status
  while true; do
    status="$(http_get_json_status "${url}" "${HTTP_TIMEOUT_SEC}")"
    if [ "${status}" = "200" ]; then
      log "upstream ready: status=200 url=${url}"
      return 0
    fi
    now="$(date +%s)"
    if [ $(( now - start_ts )) -ge "${timeout_total}" ]; then
      err "upstream not ready within timeout: last_status=${status} url=${url}"
      return 1
    fi
    sleep "${interval}"
  done
}

check_redis() {
  if [ "${CACHE_ENABLED}" != "1" ] && [ "${RATE_LIMIT_ENABLED}" != "1" ]; then
    return 0
  fi
  if [ -z "${REDIS_URL}" ]; then
    warn "redis_url not set; cache/rate-limit will fallback to memory"
    return 0
  fi
  log "probing redis: url=$(mask "${REDIS_URL}")"
  python - "${REDIS_URL}" <<'PY'
import sys
try:
    import redis  # type: ignore
except Exception:
    print("NO:redis_package_missing")
    sys.exit(0)
try:
    r = redis.Redis.from_url(sys.argv[1], decode_responses=True)
    r.ping()
    print("OK")
except Exception as e:
    print("NO:%s" % (type(e).__name__,))
PY
}

print_config() {
  log "service=${SERVICE_NAME} version=${APP_VERSION} host=${HOST} port=${PORT} log_level=${LOG_LEVEL}"
  log "lt_base_url=${LT_BASE_URL} lt_api_key=$( [ -n "${LT_API_KEY}" ] && echo set || echo unset )"
  log "allowed_langs=${ALLOWED_LANGS}"
  log "cache_enabled=${CACHE_ENABLED} cache_ttl=${CACHE_TTL_SEC}s redis_url=$(mask "${REDIS_URL}")"
  log "rate_limit_enabled=${RATE_LIMIT_ENABLED} bucket=${RATE_BUCKET} refill=${RATE_REFILL}/s"
}

# --------------------------- Flow ---------------------------
print_config

if [ "${WAIT_FOR_UPSTREAM}" = "1" ]; then
  wait_for_upstream "${WAIT_UPSTREAM_URL}" "${WAIT_UPSTREAM_TIMEOUT_SEC}" "${WAIT_UPSTREAM_INTERVAL_SEC}" || {
    warn "continuing without upstream readiness; service will return 503 on /_ready"
  }
fi

redis_probe="$(check_redis || true)"
if [ -n "${redis_probe:-}" ] && [ "${redis_probe#OK}" != "${redis_probe}" ]; then
  log "redis reachable"
elif [ -n "${redis_probe:-}" ] && [ "${redis_probe#NO:}" != "${redis_probe}" ]; then
  warn "redis not reachable or package missing: ${redis_probe#NO:}"
fi

# Vorab-App-Import als schneller Fail (optional)
python - <<'PY' || warn "preload app failed; wsgi fallback may start instead"
import importlib
mod = importlib.import_module("wsgi")
app = getattr(mod, "app", None)
assert app is not None, "wsgi.app is None"
PY

# --------------------------- Start ---------------------------
GUNICORN_BIN="gunicorn"
GUNICORN_APP="wsgi:app"
GUNICORN_CFG="gunicorn.conf.py"

cmd=( "${GUNICORN_BIN}" -c "${GUNICORN_CFG}" "${GUNICORN_APP}" )
if [ -n "${EXTRA_GUNICORN_ARGS:-}" ]; then
  # shellcheck disable=SC2206
  extra=( ${EXTRA_GUNICORN_ARGS} )
  cmd+=( "${extra[@]}" )
fi

log "starting gunicorn: ${cmd[*]}"
set +e
"${cmd[@]}" &
child_pid="$!"
set -e

wait "${child_pid}"
exit_code=$?
if [ $exit_code -ne 0 ]; then
  err "gunicorn exited with code ${exit_code}"
fi
exit "${exit_code}"
