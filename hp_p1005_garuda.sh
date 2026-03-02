#!/usr/bin/env bash
# hp_p1005_garuda.sh
#
# HP LaserJet P1005 helper for Garuda Linux (Arch-based)
#
# Logging:
#   JSON Lines only
#   hp_p1005_garuda_YYYY-MM-DD-HH-MM-SS.jsonl
#
# Behaviour:
#   - No arguments == --all
#
# Notes:
# - Garuda uses dracut by default (no mkinitcpio required)
# - system-config-printer + HPLIP is the only reliable setup on CUPS 2.4+
# - P1005 is host-based (first print uploads firmware)

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
HOSTNAME_SHORT="$(hostname -s 2>/dev/null || hostname)"
TS_NOW="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="./hp_p1005_garuda_${TS_NOW}.jsonl"
LOG_FD=3

# -------------------------
# JSON logging
# -------------------------

json_escape() {
  local s="$1"
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/\\r}
  s=${s//$'\t'/\\t}
  printf '%s' "$s"
}

log_open() {
  exec {LOG_FD}>>"$LOG_FILE"
  log_write info log_open "logging started" "path==$LOG_FILE"
}

log_close() {
  log_write info log_close "logging finished"
  exec {LOG_FD}>&-
}

log_write() {
  local level="$1"
  local event="$2"
  local msg="$3"
  shift 3 || true

  local extras=""
  while (($#)); do
    case "$1" in
      *=*)
        local k="${1%%=*}"
        local v="${1#*=}"
        extras+=",\"$(json_escape "$k")\":\"$(json_escape "$v")\""
        ;;
    esac
    shift
  done

  printf '{"ts":"%s","host":"%s","script":"%s","level":"%s","event":"%s","msg":"%s"%s}\n' \
    "$(date -Iseconds)" \
    "$(json_escape "$HOSTNAME_SHORT")" \
    "$(json_escape "$SCRIPT_NAME")" \
    "$(json_escape "$level")" \
    "$(json_escape "$event")" \
    "$(json_escape "$msg")" \
    "$extras" >&$LOG_FD
}

die() {
  echo "ERROR: $*" >&2
  log_write error fatal "$*"
  exit 1
}

info() {
  echo "==> $*"
  log_write info info "$*"
}

warn() {
  echo "WARN: $*" >&2
  log_write warn warn "$*"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Command not found: $1"
}

# -------------------------
# Actions
# -------------------------

check_pkgs() {
  info "Checking required packages"
  local missing=()
  for p in cups hplip hplip-plugin system-config-printer; do
    if pacman -Q "$p" >/dev/null 2>&1; then
      log_write info pkg_ok "$p" pkg="$p"
    else
      log_write warn pkg_missing "$p" pkg="$p"
      missing+=("$p")
    fi
  done
  if ((${#missing[@]})); then
    warn "Missing packages: ${missing[*]}"
  fi
}

check_usb() {
  info "Checking USB device"
  local line
  line="$(lsusb | grep -iE '03f0:3d17|LaserJet P1005' || true)"
  if [[ -n "$line" ]]; then
    log_write info usb_detected "printer detected" line="$line"
  else
    warn "Printer not detected via USB"
    log_write warn usb_missing "printer not detected"
  fi
}

check_cups() {
  info "Checking CUPS"
  if systemctl is-active --quiet cups; then
    log_write info cups_active "cups running"
  else
    warn "cups not running"
    log_write warn cups_inactive "cups not running"
  fi
}

check_usblp() {
  if lsmod | awk '{print $1}' | grep -qx usblp; then
    warn "usblp loaded"
    log_write warn usblp_loaded "usblp loaded"
  else
    log_write info usblp_not_loaded "usblp not loaded"
  fi
}

disable_auto() {
  info "Disabling configure-printer@.service"
  systemctl list-unit-files | grep -q '^configure-printer@\.service' \
    && systemctl disable --now configure-printer@.service || true
  log_write info auto_config_disabled "configure-printer disabled"
}

unload_usblp() {
  if lsmod | grep -q '^usblp'; then
    info "Unloading usblp"
    sudo rmmod usblp
    log_write info usblp_unloaded "usblp unloaded"
  fi
}

blacklist_usblp() {
  info "Blacklisting usblp"
  echo "blacklist usblp" | sudo tee /etc/modprobe.d/blacklist-usblp.conf >/dev/null
  log_write info usblp_blacklisted "usblp blacklisted"
}

run_gui() {
  info "Launching system-config-printer"
  sudo system-config-printer
  log_write info gui_launched "system-config-printer launched"
}

print_test() {
  info "Sending test prints"
  lp /etc/hostname || true
  sleep 2
  lp /etc/hostname || true
  log_write info test_sent "two test jobs sent"
}

# -------------------------
# Main
# -------------------------

main() {
  need_cmd pacman
  need_cmd lsusb
  need_cmd systemctl
  need_cmd lp
  need_cmd system-config-printer

  log_open

  local do_all=0

  if (($# == 0)); then
    do_all=1
  else
    for arg in "$@"; do
      [[ "$arg" == "--all" ]] && do_all=1
    done
  fi

  if ((do_all)); then
    log_write info mode "running --all"
    check_pkgs
    check_usb
    check_cups
    check_usblp
    disable_auto
    unload_usblp
    blacklist_usblp
    run_gui
    print_test
  else
    die "Only --all is supported in this version"
  fi

  log_close
  info "Done. Log: $LOG_FILE"
}

main "$@"
