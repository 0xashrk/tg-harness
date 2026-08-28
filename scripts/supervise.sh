#!/usr/bin/env bash
# Keep WARP/SOCKS :40000 and one secretary watch up. Do not change default route.
# Real WARP proxy only. A dummy local SOCKS on :40000 looks "up" while MTProto still fails.
# After rematerialize, run scripts/restore-pipeline.sh (this loop does not install packages).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/out"
LOG="$OUT/supervisor.log"
WATCH_LOG="$OUT/watch.log"
SOCKS_LOG="$OUT/socks.log"
PIDFILE="$OUT/supervisor.pid"
LOCKFILE="$OUT/supervisor.lock"
WATCH_PIDFILE="$ROOT/watch.pid"
SOCK="$ROOT/watch.sock"
VENV="$ROOT/.venv/bin/python"
export TG_HARNESS_ROLE=secretary
log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; }

socks_up() {
  command -v curl >/dev/null 2>&1 || return 1
  curl --silent --show-error --fail \
    --connect-timeout 5 --max-time 10 \
    --socks5-hostname 127.0.0.1:40000 \
    https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null |
    grep -qE '^warp=(on|plus)\r?$'
}

acquire_lock() {
  if ! command -v flock >/dev/null 2>&1; then
    log "FAILED to start supervisor: flock is required"
    return 1
  fi
  exec 9>"$LOCKFILE"
  if ! flock -n 9; then
    log "supervisor already running; refusing duplicate"
    return 1
  fi
}

ensure_socks() {
  if socks_up; then
    return 0
  fi
  log "socks 40000 down; restoring warp proxy"
  if ! command -v warp-cli >/dev/null 2>&1; then
    log "FAILED to restore socks 40000 (warp-cli missing; run scripts/restore-pipeline.sh — not a dummy local SOCKS)"
    return 1
  fi
  if ! pgrep -x warp-svc >/dev/null 2>&1; then
    if command -v systemctl >/dev/null 2>&1; then
      if command -v sudo >/dev/null 2>&1; then
        sudo -n systemctl start warp-svc >>"$SOCKS_LOG" 2>&1 || true
        sleep 1
      fi
    fi
  fi
  if ! pgrep -x warp-svc >/dev/null 2>&1; then
    if command -v sudo >/dev/null 2>&1; then
      sudo -n warp-svc >>"$SOCKS_LOG" 2>&1 &
      sleep 2
    fi
  fi
  if ! warp-cli --accept-tos registration show >>"$SOCKS_LOG" 2>&1; then
    warp-cli --accept-tos registration new >>"$SOCKS_LOG" 2>&1 || true
  fi
  warp-cli --accept-tos mode proxy >>"$SOCKS_LOG" 2>&1 || true
  warp-cli --accept-tos proxy port 40000 >>"$SOCKS_LOG" 2>&1 || true
  # Optional: this host needed MASQUE h2-only after rematerialize. Skip if unknown.
  warp-cli --accept-tos tunnel protocol set MASQUE >>"$SOCKS_LOG" 2>&1 || true
  warp-cli --accept-tos tunnel masque-options set h2-only >>"$SOCKS_LOG" 2>&1 || true
  warp-cli --accept-tos connect >>"$SOCKS_LOG" 2>&1 || true
  sleep 2
  if socks_up; then
    log "socks restored via warp-cli proxy"
    return 0
  fi
  log "FAILED to restore socks 40000 (need real WARP proxy, not a dummy local SOCKS)"
  return 1
}

python_watch_pids() {
  local f pid comm cmd
  for f in /proc/[0-9]*/cmdline; do
    cmd="$(tr '\0' ' ' < "$f" 2>/dev/null || true)"
    case "$cmd" in
      *"/tg-harness/.venv/bin/python -m tg_harness.cli watch"*|*" -m tg_harness.cli watch"*) ;;
      *) continue ;;
    esac
    pid="${f#/proc/}"; pid="${pid%/cmdline}"
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || true)"
    case "$comm" in
      python*) echo "$pid" ;;
    esac
  done
}

watch_alive() {
  local pid=""
  if [[ -f "$WATCH_PIDFILE" ]]; then
    pid="$(cat "$WATCH_PIDFILE" 2>/dev/null || true)"
  fi
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    comm="$(cat /proc/$pid/comm 2>/dev/null || true)"
    case "$comm" in
      python*)
        if [[ -S "$SOCK" ]]; then
          return 0
        fi
        ;;
    esac
  fi
  if [[ -n "$(python_watch_pids)" ]] && [[ -S "$SOCK" ]]; then
    return 0
  fi
  return 1
}

ensure_watch() {
  if watch_alive; then
    return 0
  fi
  if [[ -n "$(python_watch_pids)" ]]; then
    log "watch process exists but sock/pid unhealthy; not starting a second client"
    return 1
  fi
  if [[ -S "$SOCK" ]]; then
    rm -f "$SOCK" || true
  fi
  log "starting secretary watch"
  cd "$ROOT"
  nohup env TG_HARNESS_ROLE=secretary "$VENV" -m tg_harness.cli watch >>"$WATCH_LOG" 2>&1 &
  echo $! > "$WATCH_PIDFILE"
  sleep 2
  if watch_alive; then
    log "watch up pid=$(cat "$WATCH_PIDFILE")"
    return 0
  fi
  log "FAILED to start watch"
  return 1
}

main() {
  mkdir -p "$OUT"
  acquire_lock || return 1
  echo $$ > "$PIDFILE"
  trap 'rm -f "$PIDFILE"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  log "supervisor start pid=$$ root=$ROOT"
  while true; do
    ensure_socks || true
    ensure_watch || true
    sleep 15
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
