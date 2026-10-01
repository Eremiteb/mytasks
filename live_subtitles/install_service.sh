#!/usr/bin/env bash
# Установка/удаление systemd-юнитов live_subtitles (системные, привязаны к llama-server.service).
# Использование: ./install_service.sh [install|uninstall|status]
# SCRIPT ID / PATHS -----------------------------------------------------------
set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="/etc/systemd/system"
UNITS=(live-subtitles.service live-subtitles-update.service live-subtitles-update.timer)

# HELPERS ---------------------------------------------------------------------
die() {
  printf 'Ошибка: %s\n' "$*" >&2
  exit 1
}

# Подставляет значения в шаблон и ставит его в /etc/systemd/system (нужен sudo -A).
render_unit() {
  local name="$1" src out
  src="${SCRIPT_DIR}/systemd/${name}"
  [[ -f "${src}" ]] || src="${SCRIPT_DIR}/systemd/${name}.in"
  [[ -f "${src}" ]] || die "нет шаблона для ${name}"
  out="$(mktemp)"
  sed -e "s|@UID@|${USER_ID}|g" -e "s|@USER@|${USER_NAME}|g" -e "s|@HOME@|${USER_HOME}|g" \
    -e "s|@PROJECT@|${SCRIPT_DIR}|g" -e "s|@WAYLAND_DISPLAY@|${WAYLAND_SOCKET}|g" "${src}" > "${out}"
  sudo -A install -m 0644 "${out}" "${UNIT_DIR}/${name}"
  rm -f "${out}"
  printf 'установлен: %s/%s\n' "${UNIT_DIR}" "${name}"
}

# ARGS ------------------------------------------------------------------------
ACTION="${1:-install}"
USER_NAME="$(id -un)"
USER_ID="$(id -u)"
USER_HOME="${HOME}"
WAYLAND_SOCKET="${WAYLAND_DISPLAY:-wayland-0}"

# MAIN ------------------------------------------------------------------------
case "${ACTION}" in
  install)
    [[ -x "${SCRIPT_DIR}/venv/bin/python" ]] || die "нет venv: сначала запустите ./run.fish (он создаст окружение)"
    [[ -f /etc/systemd/system/llama-server.service ]] || die "не найден llama-server.service: сервис привязывается к llama.cpp"
    mkdir -p "${USER_HOME}/.cache/live_subtitles" "${USER_HOME}/.local/state/live_subtitles"
    for unit in "${UNITS[@]}"; do
      render_unit "${unit}"
    done
    sudo -A systemctl daemon-reload
    sudo -A systemctl enable --now live-subtitles.service live-subtitles-update.timer
    systemctl status --no-pager live-subtitles.service live-subtitles-update.timer || true
    ;;
  uninstall)
    sudo -A systemctl disable --now live-subtitles.service live-subtitles-update.timer || true
    for unit in "${UNITS[@]}"; do
      sudo -A rm -f "${UNIT_DIR}/${unit}"
    done
    sudo -A systemctl daemon-reload
    printf 'юниты удалены\n'
    ;;
  status)
    systemctl status --no-pager live-subtitles.service live-subtitles-update.timer || true
    ;;
  *)
    die "неизвестное действие '${ACTION}' (install|uninstall|status)"
    ;;
esac
