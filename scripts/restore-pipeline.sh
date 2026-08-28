#!/usr/bin/env bash
# After rematerialize: apt packages and processes are gone; /workspace stays.
# Restore real WARP proxy on 127.0.0.1:40000 and exactly one secretary watch.
# Dummy local SOCKS cannot pass Telegram MTProto. Never start one.
# Do not change the default route. Do not send Telegram. Do not start a reporter.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE="$ROOT/.cache/warp"
OUT="$ROOT/out"
WATCH_LOG="$OUT/watch.log"
WATCH_PIDFILE="$ROOT/watch.pid"
SOCK="$ROOT/watch.sock"
VENV="$ROOT/.venv/bin/python"
export TG_HARNESS_ROLE=secretary

say() { echo "$*"; }
fail() { echo "restore failed: $*" >&2; exit 1; }

port_listening() {
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | grep -q '127.0.0.1:40000' && return 0
    ss -lnt 2>/dev/null | grep -qE ':40000\b' && return 0
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -sS -m 2 -o /dev/null --connect-timeout 1 \
      -x socks5h://127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace \
      >/dev/null 2>&1 && return 0
  fi
  return 1
}

warp_cli_connected() {
  command -v warp-cli >/dev/null 2>&1 || return 1
  local st
  st="$(warp-cli --accept-tos status 2>/dev/null || true)"
  printf '%s' "$st" | grep -qiE 'status update:[[:space:]]*connected' && return 0
  printf '%s' "$st" | grep -qiE '^[[:space:]]*connected[[:space:]]*$' && return 0
  return 1
}

trace_warp_on() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -sS -m 5 -x socks5h://127.0.0.1:40000 \
    https://www.cloudflare.com/cdn-cgi/trace 2>/dev/null |
    grep -qE '^warp=(on|plus)\r?$'
}

socks_healthy() {
  port_listening || return 1
  if warp_cli_connected || trace_warp_on; then
    return 0
  fi
  return 1
}

codename() {
  if command -v lsb_release >/dev/null 2>&1; then
    lsb_release -cs
    return 0
  fi
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [[ -n "${VERSION_CODENAME:-}" ]]; then
      printf '%s\n' "$VERSION_CODENAME"
      return 0
    fi
  fi
  return 1
}

sudo_n() {
  if command -v sudo >/dev/null 2>&1; then
    sudo -n "$@"
  else
    "$@"
  fi
}

cached_deb() {
  ls -1t "$CACHE"/cloudflare-warp*.deb 2>/dev/null | head -n1 || true
}

cache_installed_deb() {
  mkdir -p "$CACHE"
  local src
  src="$(ls -1t /var/cache/apt/archives/cloudflare-warp*.deb 2>/dev/null | head -n1 || true)"
  [[ -n "$src" ]] || return 0
  cp -f "$src" "$CACHE/" || true
}

install_warp() {
  if command -v warp-cli >/dev/null 2>&1; then
    return 0
  fi
  say "warp-cli missing; installing cloudflare-warp"
  mkdir -p "$CACHE"

  local deb
  deb="$(cached_deb)"
  if [[ -n "$deb" ]]; then
    say "installing cached WARP package"
    if sudo_n dpkg -i "$deb" >/dev/null 2>&1; then
      command -v warp-cli >/dev/null 2>&1 && return 0
    fi
    sudo_n apt-get -y -f install >/dev/null 2>&1 || true
    if command -v warp-cli >/dev/null 2>&1; then
      return 0
    fi
    say "cached package install failed; trying official repo"
  fi

  command -v apt-get >/dev/null 2>&1 || fail "cloudflare-warp not installed (no apt-get, no working cache)"
  local code
  code="$(codename || true)"
  [[ -n "$code" ]] || fail "cannot detect Debian/Ubuntu codename for the Cloudflare repo"

  if ! sudo_n mkdir -p /usr/share/keyrings >/dev/null 2>&1; then
    fail "cannot write apt keyring (need passwordless sudo)"
  fi
  if ! curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg |
    sudo_n gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
  then
    fail "could not install Cloudflare apt key"
  fi
  if ! printf 'deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ %s main\n' "$code" |
    sudo_n tee /etc/apt/sources.list.d/cloudflare-client.list >/dev/null
  then
    fail "could not add Cloudflare apt repo"
  fi
  sudo_n apt-get update -y >/dev/null 2>&1 || fail "apt-get update failed for Cloudflare repo"
  sudo_n apt-get install -y cloudflare-warp >/dev/null 2>&1 || fail "apt-get install cloudflare-warp failed"
  cache_installed_deb
  command -v warp-cli >/dev/null 2>&1 || fail "warp-cli still missing after install"
  say "cloudflare-warp installed; .deb cached under .cache/warp/"
}

start_warp_svc() {
  if pgrep -x warp-svc >/dev/null 2>&1; then
    return 0
  fi
  if command -v systemctl >/dev/null 2>&1; then
    sudo_n systemctl start warp-svc >/dev/null 2>&1 || true
    sleep 1
  fi
  if pgrep -x warp-svc >/dev/null 2>&1; then
    return 0
  fi
  if command -v service >/dev/null 2>&1; then
    sudo_n service warp-svc start >/dev/null 2>&1 || true
    sleep 1
  fi
  if pgrep -x warp-svc >/dev/null 2>&1; then
    return 0
  fi
  sudo_n warp-svc >/dev/null 2>&1 &
  sleep 2
  pgrep -x warp-svc >/dev/null 2>&1
}

warp_try() {
  # Optional CLI knobs (MASQUE/h2) must not fail the restore.
  warp-cli --accept-tos "$@" >/dev/null 2>&1 || return 0
}

restore_warp_proxy() {
  if socks_healthy; then
    say "socks 127.0.0.1:40000 already healthy"
    return 0
  fi
  say "socks 127.0.0.1:40000 down; restoring WARP proxy-only"
  install_warp
  start_warp_svc || fail "warp-svc failed to start"

  if ! warp-cli --accept-tos registration show >/dev/null 2>&1; then
    warp-cli --accept-tos registration new >/dev/null 2>&1 || fail "warp-cli registration new failed"
  fi

  warp-cli --accept-tos mode proxy >/dev/null 2>&1 || fail "warp-cli mode proxy failed"
  warp-cli --accept-tos proxy port 40000 >/dev/null 2>&1 || fail "warp-cli proxy port 40000 failed"
  warp_try tunnel protocol set MASQUE
  warp_try tunnel masque-options set h2-only
  warp-cli --accept-tos connect >/dev/null 2>&1 || fail "warp-cli connect failed"

  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if port_listening && { warp_cli_connected || trace_warp_on; }; then
      say "WARP proxy listening on 127.0.0.1:40000"
      return 0
    fi
    sleep 1
  done

  if port_listening && warp_cli_connected; then
    say "WARP proxy listening on 127.0.0.1:40000 (trace skipped or flaky)"
    return 0
  fi
  fail "WARP proxy not healthy (need real WARP, not a dummy SOCKS)"
}

ensure_venv() {
  if [[ -x "$VENV" ]] && "$VENV" -c "import telethon" >/dev/null 2>&1; then
    say "venv ok"
    return 0
  fi
  say "recreating .venv"
  if ! python3 -m venv "$ROOT/.venv" >/dev/null 2>&1; then
    sudo_n apt-get update -y >/dev/null 2>&1 || true
    sudo_n apt-get install -y python3-venv python3-pip >/dev/null 2>&1 || true
    python3 -m venv "$ROOT/.venv" >/dev/null 2>&1 || fail "could not create .venv"
  fi
  if [[ -f "$ROOT/requirements.txt" ]]; then
    "$ROOT/.venv/bin/pip" install -q -r "$ROOT/requirements.txt" || fail "pip install -r requirements.txt failed"
  else
    "$ROOT/.venv/bin/pip" install -q -e "$ROOT" || fail "pip install from pyproject failed"
  fi
  "$VENV" -c "import telethon" >/dev/null 2>&1 || fail "import telethon still fails"
  say "venv ready"
}

python_watch_pids() {
  local f pid comm cmd
  for f in /proc/[0-9]*/cmdline; do
    cmd="$(tr '\0' ' ' < "$f" 2>/dev/null || true)"
    case "$cmd" in
      *" -m tg_harness.cli watch"*) ;;
      *) continue ;;
    esac
    pid="${f#/proc/}"
    pid="${pid%/cmdline}"
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || true)"
    case "$comm" in
      python*) echo "$pid" ;;
    esac
  done
}

watch_process_up() {
  local pid=""
  if [[ -f "$WATCH_PIDFILE" ]]; then
    pid="$(cat "$WATCH_PIDFILE" 2>/dev/null || true)"
  fi
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    case "$(cat "/proc/$pid/comm" 2>/dev/null || true)" in
      python*) return 0 ;;
    esac
  fi
  [[ -n "$(python_watch_pids)" ]]
}

ensure_watch() {
  if watch_process_up && [[ -S "$SOCK" ]]; then
    say "secretary watch already up; not starting another"
    return 0
  fi
  if [[ -n "$(python_watch_pids)" ]]; then
    fail "a secretary watch is already running; not starting a second client"
  fi
  if [[ -S "$SOCK" ]]; then
    rm -f "$SOCK" || true
  fi
  [[ -x "$VENV" ]] || fail "venv python missing; cannot start watch"
  mkdir -p "$OUT"
  say "starting secretary watch"
  cd "$ROOT"
  nohup env TG_HARNESS_ROLE=secretary "$VENV" -m tg_harness.cli watch >>"$WATCH_LOG" 2>&1 &
  echo $! > "$WATCH_PIDFILE"
  local i
  for i in $(seq 1 60); do
    if [[ -S "$SOCK" ]]; then
      say "watch.sock ready pid=$(cat "$WATCH_PIDFILE")"
      return 0
    fi
    sleep 1
  done
  fail "watch started but watch.sock never appeared"
}

query_status() {
  [[ -S "$SOCK" ]] || fail "watch.sock missing; not opening a second client for status"
  local raw
  if ! raw="$(cd "$ROOT" && env TG_HARNESS_ROLE=secretary "$VENV" -m tg_harness.cli status 2>/dev/null)"; then
    fail "cli status failed"
  fi
  # Non-secret JSON: authorized / via / role only.
  say "status $raw"
  printf '%s' "$raw" | "$VENV" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(2)
if not data.get("authorized"):
    sys.exit(1)
if data.get("via") != "watch":
    sys.exit(3)
' || fail "session not authorized via watch"
}

main() {
  mkdir -p "$OUT" "$CACHE"
  restore_warp_proxy
  ensure_venv
  ensure_watch
  query_status
  say "restore ok socks=127.0.0.1:40000 watch=up"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
