#!/opt/bin/bash
###############################################################################
# QNAP-версия cloud_backup.sh
#
# Резервное копирование /opt/esimych-cloud с удалённого сервера через
# WireGuard VPN, запускаемая НА САМОМ QNAP NAS (Task Scheduler / Entware cron).
#
# Отличия от десктопной версии (cloud_backup.sh):
#   - shebang указывает на Entware bash (/opt/bin/bash) напрямую — обычный
#     на QNAP #!/bin/sh/#!/bin/ash не поддерживает массивы/local/${!var},
#     которые использует скрипт. Если Entware ставит bash по другому пути,
#     поправьте shebang или создайте симлинк: ln -s <путь> /opt/bin/bash;
#   - PATH дополнен /opt/sbin:/opt/bin (Entware), т.к. QTS сам по себе не
#     содержит wg-quick, wg, zstd, sqlite3 и т.п.;
#   - убран весь sudo-код: Task Scheduler и Entware cron на QNAP и так
#     выполняют скрипт от root, sudo не требуется и обычно не установлен;
#   - подсказки по установке недостающих утилит даются через opkg, а не pacman.
#
# ПРЕДВАРИТЕЛЬНАЯ НАСТРОЙКА НА QNAP (сделать один раз вручную):
#   1. Установить Entware по SSH (App Center/QNAP Club у части моделей и
#      прошивок не подключается как репозиторий — рабочий вариант через SSH,
#      без App Center). Для aarch64 (в т.ч. этот NAS) используется сборка
#      aarch64-k3.10 — она ABI-совместима и с более новыми ядрами aarch64:
#        ssh admin@<IP-QNAP>
#        mkdir -p /share/Public/entware   # постоянное хранилище на диске
#        mkdir -p /opt
#        mount --bind /share/Public/entware /opt
#        wget -O /opt/generic.sh https://bin.entware.net/aarch64-k3.10/installer/generic.sh
#        sh /opt/generic.sh
#      /opt — это bind-mount, он слетает при каждой перезагрузке NAS (сами
#      файлы в /share/Public/entware остаются целы). Сам этот скрипт при
#      каждом запуске проверяет и при необходимости переподключает /opt
#      (см. ensure_entware_mount ниже) — поэтому отдельная задача "на
#      загрузку" в Планировщике НЕ обязательна, достаточно, чтобы сам
#      cloud_backup_qnap.sh запускался по расписанию (см. пункт 6).
#      Если позже всё же получится подключить репозиторий QNAP Club и
#      установить Entware через App Center — тоже подходит, шаг 1 просто
#      можно пропустить.
#   2. Установить нужные пакеты — в ТОЙ ЖЕ SSH-сессии на QNAP, сразу после
#      generic.sh (opkg — это /opt/bin/opkg, устанавливается им же). В свежей
#      SSH-сессии /opt/bin и /opt/sbin ещё не в PATH (это нормально — сам
#      Entware добавляет их в PATH только через /opt/etc/profile при НОВОМ
#      входе по SSH), поэтому КАЖДЫЙ РАЗ в начале сессии, где нужно вручную
#      вызывать opkg/wireguard-go/wg, сначала выполните:
#        export PATH="/opt/sbin:/opt/bin:$PATH"
#      и только потом:
#        opkg update
#        opkg install bash wireguard-tools wireguard-go ncat xxd zstd sqlite3-cli coreutils-stat
#      Если после export PATH команда "opkg" всё ещё не находится — значит
#      шаг 1 (generic.sh) не завершился успешно, проверьте наличие файла
#      /opt/bin/opkg.
#      ВАЖНО (обнаружено на реальном железе, QTS с ядром 4.2.8 aarch64):
#      консольная утилита wg(8) (а значит и wg-quick) на части устройств НЕ
#      РАБОТАЕТ в принципе — "wg-quick up"/"wg setconf"/"wg show" стабильно
#      падают с "Unable to modify/access interface: Protocol not supported",
#      даже когда сам wireguard-go, TUN-устройство и его unix-сокет UAPI
#      полностью исправны. Похоже, ядро регистрирует netlink-family
#      "wireguard" (поэтому и wg, и wireguard-go считают, что доступна
#      нативная поддержка ядра), но сама реализация в ядре нерабочая/
#      урезанная — из-за этого не происходит отката на userspace-сокет.
#      Поэтому этот скрипт вообще НЕ вызывает ни "wg", ни "wg-quick" — он
#      сам поднимает туннель напрямую поверх userspace UAPI-сокета
#      wireguard-go через ncat (см. функции wg_up_userspace/wg_conf_get в
#      коде ниже); wireguard-tools по-прежнему нужен только для генерации
#      ключей (wg genkey/wg pubkey, шаг 3) — это чисто криптографические
#      операции, сетевого стека не касаются и от этой проблемы не страдают.
#      ВАЖНО: пакета sshpass в репозитории Entware для этой архитектуры
#      (aarch64-k3.10) НЕТ ("opkg install sshpass" -> "Unknown package").
#      Поэтому QNAP-версия скрипта подключается к серверу по SSH-ключу, а
#      не по паролю (это ещё и безопаснее, чем хранить пароль в conf).
#      Сама настройка ключа делается ПОЗЖЕ, в шаге 5 — она требует уже
#      поднятого WireGuard-туннеля, т.к. 10.66.66.1 доступен только через
#      него, а до шага 4 туннеля ещё нет.
#   3. Создать конфиг WireGuard-клиента для ЭТОГО NAS (нельзя переиспользовать
#      приватный ключ и IP от десктопного клиента — на сервере должен быть
#      заведён отдельный peer с уникальным ключом и адресом, например
#      10.66.66.3/32).
#      НЕ используйте vi/вставку из буфера обмена — так легко занести CRLF
#      (\r\n) или BOM (невидимые байты EF BB BF в начале файла), из-за
#      которых wg-quick не распознаёт "[Interface]" как заголовок секции и
#      падает с "Line unrecognized: ..." / "Configuration parsing error".
#      Создайте файл через heredoc прямо в shell (ключи и IP свои):
#        mkdir -p /opt/etc/wireguard
#        cat > /opt/etc/wireguard/wg0-qnap.conf << 'EOF'
#        [Interface]
#        PrivateKey = <приватный ключ QNAP>
#        Address = 10.66.66.3/32
#
#        [Peer]
#        PublicKey = <публичный ключ сервера>
#        Endpoint = <адрес сервера>:<порт>
#        AllowedIPs = 10.66.66.0/24
#        PersistentKeepalive = 25
#        EOF
#        chmod 600 /opt/etc/wireguard/wg0-qnap.conf
#      Если конфиг всё же правился в vi/скопирован откуда-то ещё и wg-quick
#      падает с "Line unrecognized" — проверьте на BOM/CRLF и пересоздайте
#      именно heredoc-способом выше:
#        head -c3 /opt/etc/wireguard/wg0-qnap.conf | xxd   # "ef bb bf" = BOM
#        cat -A /opt/etc/wireguard/wg0-qnap.conf           # "^M" в конце строк = CRLF
#      ВАЖНО: чаще всего "Line unrecognized: `PrivateKey=...'" означает не
#      BOM/CRLF, а просто ПРОПУЩЕННУЮ строку "[Interface]" в начале файла
#      (например, случайно не скопировалась при вставке) — тогда PrivateKey/
#      Address/DNS остаются без секции и wg-quick передаёт их как есть в
#      "wg setconf", где они уже не распознаются. Проверьте, что файл
#      начинается именно с "[Interface]" (см. cat -A выше).
#   4. Проверить вручную (та же оговорка про PATH, что и в шаге 2):
#        export PATH="/opt/sbin:/opt/bin:$PATH"
#      Сам скрипт НЕ вызывает wg/wg-quick (см. ВАЖНО в шаге 2) — он поднимает
#      туннель напрямую через userspace UAPI-сокет wireguard-go. Проверить,
#      что на вашем NAS этот путь работает, можно так:
#        wireguard-go wg0-qnap
#        printf 'get=1\n\n' | ncat -U /var/run/wireguard/wg0-qnap.sock
#      Ответ "errno=0" (пусть даже остальные поля пустые/нулевые) означает,
#      что демон и сокет исправны — именно на этом основана реализация в
#      скрипте. Если вместо этого "connection refused"/"No such file or
#      directory" — wireguard-go не создал сокет, чаще всего из-за
#      отсутствия TUN:
#        ls -la /dev/net/tun
#      Если файла нет — создать:
#        mkdir -p /dev/net
#        mknod /dev/net/tun c 10 200
#        chmod 600 /dev/net/tun
#      Реальный подъём туннеля со всеми ключами и маршрутом делает сам
#      cloud_backup_qnap.sh при запуске (функция wg_up_userspace) — отдельно
#      руками конфигурировать пира не нужно, достаточно запустить скрипт.
#      Оставшийся wireguard-go после этой ручной проверки можно остановить:
#        kill $(ps w | grep '[w]ireguard-go wg0-qnap' | awk '{print $1}')
#   5. Только теперь, когда туннель поднят и 10.66.66.1 отвечает на ping,
#      скопировать публичный SSH-ключ на удалённый сервер (ssh-copy-id в
#      Entware нет ни в одном пакете — копируем вручную, по отдельным
#      командам, каждая может спросить пароль <REMOTE_USER> — это нормально,
#      он нужен только для этого разового копирования):
#        ssh -p <REMOTE_SSH_PORT> <REMOTE_USER>@10.66.66.1 "mkdir -p ~/.ssh && chmod 700 ~/.ssh"
#        cat /share/Public/entware/ssh-qnap/id_ed25519.pub > /tmp/qnap_key.pub
#        scp -P <REMOTE_SSH_PORT> /tmp/qnap_key.pub <REMOTE_USER>@10.66.66.1:/tmp/qnap_key.pub
#        ssh -p <REMOTE_SSH_PORT> <REMOTE_USER>@10.66.66.1
#        # (на сервере, внутри этой SSH-сессии:)
#        #   cat /tmp/qnap_key.pub >> ~/.ssh/authorized_keys
#        #   chmod 600 ~/.ssh/authorized_keys
#        #   rm -f /tmp/qnap_key.pub
#        #   exit
#      Путь к приватному ключу укажите в conf/cloud_backup_qnap.conf как
#      REMOTE_SSH_KEY; REMOTE_PASSWORD можно оставить пустым — скрипт сам
#      выберет sshpass+пароль (если он всё же установлен) либо SSH-ключ.
#   6. Заполнить conf/cloud_backup_qnap.conf (см. .example) с
#      WG_INTERFACE="wg0-qnap".
#   7. Запланировать запуск САМОГО этого скрипта. Варианты (если не находите
#      Планировщик задач — попробуйте иконку поиска (лупа) вверху рабочего
#      стола QTS и наберите "Task Scheduler"/"Планировщик задач"; на части
#      моделей/прошивок пункт есть только в Панели управления > Система, и
#      виден лишь у admin-аккаунта):
#        a) Панель управления > Система > Планировщик задач > "Задание,
#           определяемое пользователем" (штатный способ, если раздел найден);
#        b) если раздела нет вообще — персистентный crontab QNAP (в отличие
#           от обычного Linux, /etc/config/crontab переживает перезагрузку,
#           т.к. хранится в конфигурации прошивки):
#             vi /etc/config/crontab
#             # добавить строку (каждый день в 21:00):
#             0 21 * * * /ПОЛНЫЙ/ПУТЬ/cloud_backup_qnap.sh
#             crontab /etc/config/crontab
#             /etc/init.d/crond.sh restart
###############################################################################

set -uo pipefail

# Entware-утилиты (wg-quick, wg, zstd, sqlite3 и т.д.) лежат в /opt/*bin,
# которых обычно нет в PATH при запуске из Task Scheduler/cron.
export PATH="/opt/sbin:/opt/bin:${PATH}"

# Каталог с постоянными данными Entware на диске (см. шаг 1 выше). Если /opt
# ещё не примонтирован (например, после перезагрузки NAS, а отдельной задачи
# "на загрузку" в Планировщике нет), переподключаем его здесь же — тогда
# для работы скрипта достаточно, чтобы ОН САМ запускался по расписанию.
ENTWARE_DATA_DIR="${ENTWARE_DATA_DIR:-/share/Public/entware}"
ensure_entware_mount() {
  [ -x /opt/bin/opkg ] && return 0
  [ -d "$ENTWARE_DATA_DIR/etc" ] || return 0
  mkdir -p /opt
  mount --bind "$ENTWARE_DATA_DIR" /opt 2>/dev/null
  [ -x /opt/etc/init.d/rc.unslung ] && /opt/etc/init.d/rc.unslung start >/dev/null 2>&1
}
ensure_entware_mount

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE="${SCRIPT_NAME%.*}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_DIR="${SCRIPT_DIR}/conf"
CONFIG_FILE="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"
LOG_DIR="${SCRIPT_DIR}/logs"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
PROGRESS_METRICS_FILE=""

mkdir -p "$LOG_DIR"

if [ -r "$LOG_TEMPLATE_FILE" ]; then
  # shellcheck source=/dev/null
  source "$LOG_TEMPLATE_FILE"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

json_escape() {
  # Удаляем/нормализуем управляющие символы, чтобы каждая запись оставалась
  # корректной одной строкой JSONL даже при шумном stderr внешних команд.
  printf '%s' "$1" \
    | tr '\n\t' '  ' \
    | tr -d '\000-\010\013\014\016-\037' \
    | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g'
}

log_json() {
  local level="$1"
  local event="$2"
  local msg="$3"
  local detail="${4:-}"
  local rc="${5:-null}"
  local ts_val msg_esc detail_esc level_norm
  ts_val="$(ts)"
  msg_esc="$(json_escape "$msg")"
  detail_esc="$(json_escape "$detail")"
  level_norm="$(printf '%s' "$level" | tr '[:upper:]' '[:lower:]')"
  printf '{"@timestamp":"%s","ts":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
    "$ts_val" "$ts_val" "$LOG_SCHEMA_VERSION" "$LOG_COMPAT_TARGETS" "$level_norm" "$msg_esc" "$event" "$SCRIPT_BASE" "$SCRIPT_NAME" "$event" "$level_norm" "$msg_esc" "$detail_esc" "$rc" >> "$LOG_FILE"
}

cleanup_logs() {
  local old_logs
  old_logs=$(ls -1t "${LOG_DIR}/${SCRIPT_BASE}-"*.jsonl 2>/dev/null | tail -n +6)
  [ -n "$old_logs" ] && rm -f $old_logs
}

VALIDATE_BACKUP_DETAIL=""

validate_backup_file() {
  local backup_path="$1"
  local validate_out rc

  VALIDATE_BACKUP_DETAIL=""

  if [ ! -s "$backup_path" ]; then
    VALIDATE_BACKUP_DETAIL="file is empty"
    return 1
  fi

  case "$backup_path" in
    *.tar.gz)
      if ! command -v gzip >/dev/null 2>&1; then
        VALIDATE_BACKUP_DETAIL="gzip not found"
        return 2
      fi
      validate_out=$(gzip -t "$backup_path" 2>&1)
      rc=$?
      VALIDATE_BACKUP_DETAIL="$validate_out"
      return $rc
      ;;
    *.tar.zst)
      if ! command -v zstd >/dev/null 2>&1; then
        VALIDATE_BACKUP_DETAIL="zstd not found (opkg install zstd)"
        return 2
      fi
      validate_out=$(zstd -t "$backup_path" 2>&1)
      rc=$?
      VALIDATE_BACKUP_DETAIL="$validate_out"
      return $rc
      ;;
    *)
      VALIDATE_BACKUP_DETAIL="unsupported backup extension"
      return 2
      ;;
  esac
}

###############################################################################
# CONFIG
###############################################################################
if [ ! -r "$CONFIG_FILE" ]; then
  echo "Ошибка: конфигурационный файл не найден: $CONFIG_FILE" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

for var in WG_INTERFACE REMOTE_HOST REMOTE_USER BACKUP_DIR; do
  if [ -z "${!var:-}" ]; then
    echo "Ошибка: переменная $var не задана в $CONFIG_FILE" >&2
    exit 1
  fi
done

# REMOTE_PASSWORD необязателен: в репозитории Entware для этой архитектуры
# нет пакета sshpass, поэтому основной способ авторизации на QNAP — SSH-ключ
# (REMOTE_SSH_KEY). Если sshpass всё же установлен и REMOTE_PASSWORD задан,
# используется он, как в десктопной версии (см. ssh_remote() ниже).
REMOTE_PASSWORD="${REMOTE_PASSWORD:-}"
REMOTE_SSH_KEY="${REMOTE_SSH_KEY:-}"
REMOTE_PATH="${REMOTE_PATH:-/opt/esimych-cloud}"
REMOTE_SSH_PORT="${REMOTE_SSH_PORT:-22}"
WG_KEEP_UP="${WG_KEEP_UP:-0}"
BACKUP_DEGRADATION_MIBS_THRESHOLD="${BACKUP_DEGRADATION_MIBS_THRESHOLD:-6}"
OPTIMIZE_SQLITE_BEFORE_BACKUP="${OPTIMIZE_SQLITE_BEFORE_BACKUP:-0}"
SQLITE_OPTIMIZE_TIMEOUT_SEC="${SQLITE_OPTIMIZE_TIMEOUT_SEC:-1800}"
OPTIMIZE_MARIADB_BEFORE_BACKUP="${OPTIMIZE_MARIADB_BEFORE_BACKUP:-0}"
MARIADB_SERVICE_NAME="${MARIADB_SERVICE_NAME:-mariadb}"
MARIADB_PURGE_BINLOGS="${MARIADB_PURGE_BINLOGS:-0}"
MARIADB_TRUNCATE_GENERAL_LOG="${MARIADB_TRUNCATE_GENERAL_LOG:-1}"
OPTIMIZE_REDIS_BEFORE_BACKUP="${OPTIMIZE_REDIS_BEFORE_BACKUP:-0}"
REDIS_SERVICE_NAME="${REDIS_SERVICE_NAME:-redis}"
REDIS_REWRITE_WAIT_SEC="${REDIS_REWRITE_WAIT_SEC:-180}"

###############################################################################
# WireGuard
# (На QNAP скрипт всегда выполняется от root — через Task Scheduler или
# Entware cron, поэтому sudo не используется.)
#
# ВАЖНО (см. подробности в шапке файла, шаги 2 и 4): на этом железе wg(8) и
# wg-quick не работают в принципе ("Protocol not supported" даже при полностью
# исправном wireguard-go/TUN/сокете) — поэтому туннель поднимается напрямую
# через userspace UAPI-сокет wireguard-go (ncat -U), без вызова wg/wg-quick.
# В ОС добавляется только узкий маршрут до REMOTE_HOST/32 (без policy-routing
# и без полного tunnel'я через 0.0.0.0/0) — значение AllowedIPs из конфига на
# это не влияет, используется намеренно только REMOTE_HOST.
###############################################################################
WG_CONF_FILE="/opt/etc/wireguard/${WG_INTERFACE:-wg0-qnap}.conf"
WG_SOCK="/var/run/wireguard/${WG_INTERFACE:-wg0-qnap}.sock"
WG_BROUGHT_UP=0
SERVICES_STOPPED=0

# Извлекает значение "Key = value" из конфига WireGuard (регистр ключа не
# важен: PrivateKey/privatekey и т.п. распознаются одинаково).
wg_conf_get() {
  local key_lc
  key_lc=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  awk -F= -v k="$key_lc" '
    {
      line = $0; keypart = $1
      gsub(/^[ \t]+|[ \t]+$/, "", keypart)
      if (tolower(keypart) == k) {
        sub(/^[^=]*=/, "", line)
        gsub(/^[ \t]+|[ \t]+$/, "", line)
        print line
        exit
      }
    }' "$WG_CONF_FILE"
}

# Конвертирует base64-ключ WireGuard в hex, как того требует UAPI-протокол
# (https://www.wireguard.com/xplatform/).
wg_b64_to_hex() {
  printf '%s' "$1" | base64 -d 2>/dev/null | xxd -p -c 256 | tr -d '\n'
}

# true, если туннель уже поднят (сокет есть и интерфейс в состоянии UP) —
# замена сломанного "wg show" (см. ВАЖНО выше).
wg_is_up() {
  [ -S "$WG_SOCK" ] || return 1
  ip link show "$WG_INTERFACE" 2>/dev/null | grep -q "state UP" || return 1
  return 0
}

# Поднимает WireGuard в обход wg/wg-quick: запускает wireguard-go, настраивает
# пира через сырой UAPI поверх ncat -U и назначает адрес/маршрут штатной ip(8).
wg_up_userspace() {
  local priv_b64 addr peer_pub_b64 psk_b64 endpoint keepalive
  local priv_hex peer_pub_hex uapi_tmp set_out i

  [ -r "$WG_CONF_FILE" ] || {
    log_json "ERROR" "wg_conf_missing" "Конфиг WireGuard не найден" "$WG_CONF_FILE"
    return 1
  }

  priv_b64=$(wg_conf_get PrivateKey)
  addr=$(wg_conf_get Address)
  peer_pub_b64=$(wg_conf_get PublicKey)
  psk_b64=$(wg_conf_get PresharedKey)
  endpoint=$(wg_conf_get Endpoint)
  keepalive=$(wg_conf_get PersistentKeepalive)
  keepalive="${keepalive:-25}"

  if [ -z "$priv_b64" ] || [ -z "$addr" ] || [ -z "$peer_pub_b64" ] || [ -z "$endpoint" ]; then
    log_json "ERROR" "wg_conf_incomplete" "В конфиге отсутствуют обязательные поля" \
      "нужны PrivateKey, Address, PublicKey и Endpoint в ${WG_CONF_FILE}"
    return 1
  fi

  priv_hex=$(wg_b64_to_hex "$priv_b64")
  peer_pub_hex=$(wg_b64_to_hex "$peer_pub_b64")

  rm -f "$WG_SOCK" 2>/dev/null
  ip link delete "$WG_INTERFACE" 2>/dev/null

  WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 wireguard-go "$WG_INTERFACE" >/dev/null 2>&1

  i=0
  while [ ! -S "$WG_SOCK" ] && [ "$i" -lt 25 ]; do
    sleep 0.2
    i=$((i + 1))
  done
  if [ ! -S "$WG_SOCK" ]; then
    log_json "ERROR" "wireguard_go_no_socket" "wireguard-go не создал UAPI-сокет" "$WG_SOCK"
    return 1
  fi

  uapi_tmp="$(mktemp)"
  {
    echo "set=1"
    echo "private_key=${priv_hex}"
    echo "listen_port=0"
    echo "replace_peers=true"
    echo "public_key=${peer_pub_hex}"
    [ -n "$psk_b64" ] && echo "preshared_key=$(wg_b64_to_hex "$psk_b64")"
    echo "endpoint=${endpoint}"
    echo "persistent_keepalive_interval=${keepalive}"
    echo "replace_allowed_ips=true"
    echo "allowed_ip=${REMOTE_HOST}/32"
    echo ""
  } > "$uapi_tmp"
  set_out=$(ncat -U "$WG_SOCK" < "$uapi_tmp" 2>&1)
  rm -f "$uapi_tmp"

  if ! printf '%s' "$set_out" | grep -q '^errno=0'; then
    log_json "ERROR" "wg_uapi_set_failed" "UAPI 'set' вернул ошибку" "$set_out"
    return 1
  fi

  ip address add "$addr" dev "$WG_INTERFACE" 2>/dev/null
  ip link set mtu 1420 up dev "$WG_INTERFACE" 2>&1
  ip route replace "${REMOTE_HOST}/32" dev "$WG_INTERFACE" 2>&1

  return 0
}

# Останавливает туннель: завершает wireguard-go (интерфейс и сокет исчезают
# вместе с процессом), подчищает сокет-файл, если он вдруг остался.
wg_down_userspace() {
  local pid
  pid=$(ps w 2>/dev/null | grep "[w]ireguard-go ${WG_INTERFACE}" | awk '{print $1}')
  [ -n "$pid" ] && kill "$pid" 2>/dev/null
  rm -f "$WG_SOCK" 2>/dev/null
  return 0
}

wg_down_if_needed() {
  if [ "$WG_BROUGHT_UP" -eq 1 ] && [ "${WG_KEEP_UP:-0}" -ne 1 ]; then
    log_json "INFO" "wg_down" "Останавливаем WireGuard ${WG_INTERFACE}..."
    wg_down_userspace && \
      log_json "INFO" "wg_down_ok" "WireGuard ${WG_INTERFACE} остановлен" || \
      log_json "WARN" "wg_down_fail" "Не удалось остановить WireGuard ${WG_INTERFACE}"
  fi
}

services_start_if_needed() {
  if [ "$SERVICES_STOPPED" -eq 1 ]; then
    SERVICES_STOPPED=0
    log_json "INFO" "services_start" "Запускаем сервисы на ${REMOTE_HOST}..."
    start_err=$(ssh_remote \
      "cd '${REMOTE_PATH}' && docker compose up -d" 2>&1)
    start_rc=$?
    if [ $start_rc -ne 0 ]; then
      log_json "WARN" "services_start_failed" "Не удалось запустить сервисы" "$start_err" $start_rc
    else
      log_json "INFO" "services_start_ok" "Сервисы запущены" "" $start_rc
    fi
  fi
}

cleanup() {
  services_start_if_needed
  wg_down_if_needed
  [ -n "$PROGRESS_METRICS_FILE" ] && rm -f "$PROGRESS_METRICS_FILE" 2>/dev/null
  cleanup_logs
}
trap cleanup EXIT INT TERM

log_json "INFO" "start" "Запуск резервного копирования (QNAP)"

if wg_is_up; then
  log_json "INFO" "wg_status" "WireGuard ${WG_INTERFACE} уже активен"
else
  log_json "INFO" "wg_up" "Поднимаем WireGuard ${WG_INTERFACE}..."
  if ! wg_up_userspace; then
    log_json "ERROR" "wg_up_failed" "Не удалось поднять WireGuard ${WG_INTERFACE}"
    echo "Ошибка: не удалось поднять WireGuard ${WG_INTERFACE}" >&2
    exit 1
  fi
  WG_BROUGHT_UP=1
  sleep 2
  log_json "INFO" "wg_up_ok" "WireGuard ${WG_INTERFACE} успешно поднят" "" 0
fi

# Проверяем реальную связь с удалённым хостом через VPN
log_json "INFO" "wg_check" "Проверяем доступность ${REMOTE_HOST} через VPN..."
_wg_ok=0
for _attempt in 1 2 3; do
  if ping -c 1 -W 5 "$REMOTE_HOST" >/dev/null 2>&1; then
    _wg_ok=1
    break
  fi
  log_json "WARN" "wg_ping_retry" "Попытка ${_attempt}/3: хост ${REMOTE_HOST} не отвечает"
  [ "$_attempt" -lt 3 ] && sleep 5
done
if [ "$_wg_ok" -eq 0 ]; then
  log_json "ERROR" "wg_no_connection" "Хост ${REMOTE_HOST} недоступен через VPN после 3 попыток" "" 1
  echo "Ошибка: ${REMOTE_HOST} недоступен через VPN" >&2
  exit 1
fi
log_json "INFO" "wg_connected" "Соединение с ${REMOTE_HOST} подтверждено"

###############################################################################
# PREFLIGHT
###############################################################################
for _wg_dep in wireguard-go ncat xxd; do
  if ! command -v "$_wg_dep" >/dev/null 2>&1; then
    log_json "ERROR" "wg_dep_missing" "Не найдена зависимость для подъёма WireGuard" "opkg install $_wg_dep"
    echo "Ошибка: не найдена команда $_wg_dep. Установите: opkg install $_wg_dep" >&2
    exit 1
  fi
done

if [ -n "$REMOTE_PASSWORD" ] && ! command -v sshpass >/dev/null 2>&1; then
  log_json "WARN" "sshpass_missing" "REMOTE_PASSWORD задан, но sshpass не найден — будет использован ssh по ключу" \
    "opkg install sshpass недоступен для этой архитектуры; настройте REMOTE_SSH_KEY"
fi

mkdir -p "$BACKUP_DIR" 2>/dev/null
if [ ! -d "$BACKUP_DIR" ]; then
  log_json "ERROR" "backup_dir_missing" "Папка назначения недоступна" "$BACKUP_DIR"
  echo "Ошибка: папка $BACKUP_DIR недоступна" >&2
  exit 1
fi

###############################################################################
# BACKUP
###############################################################################
BACKUP_DATE="$(date '+%Y-%m-%d')"
REMOTE_PARENT="$(dirname "$REMOTE_PATH")"
REMOTE_DIR="$(basename "$REMOTE_PATH")"

# Встроенные исключения для явно восстановимых данных (кэши/preview/tmp).
# Эти пути исключаются всегда и не настраиваются через conf.
REMOTE_EXCLUDES=(
  "${REMOTE_DIR}/tmp/*"
  "${REMOTE_DIR}/cache/*"
  "${REMOTE_DIR}/.cache/*"
  "${REMOTE_DIR}/data/*/uploads/*"
  "${REMOTE_DIR}/data/*/files_trashbin/uploads/*"
  "${REMOTE_DIR}/data/*/cache/*"
  "${REMOTE_DIR}/data/appdata_*/preview/*"
  "${REMOTE_DIR}/data/appdata_*/thumbnails/*"
  "${REMOTE_DIR}/data/appdata_*/css/*"
  "${REMOTE_DIR}/data/appdata_*/js/*"
)
REMOTE_TAR_EXCLUDE_ARGS=""
for _ex in "${REMOTE_EXCLUDES[@]}"; do
  REMOTE_TAR_EXCLUDE_ARGS+=" --exclude='${_ex}'"
done

SSH_OPTS="-p ${REMOTE_SSH_PORT} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=30 -o BatchMode=no -o Compression=no -c aes128-gcm@openssh.com,chacha20-poly1305@openssh.com,aes256-gcm@openssh.com,aes128-ctr"
[ -n "$REMOTE_SSH_KEY" ] && SSH_OPTS="${SSH_OPTS} -i ${REMOTE_SSH_KEY}"

# Выполняет команду на удалённом хосте: sshpass+пароль, если sshpass найден и
# задан REMOTE_PASSWORD (как в десктопной версии), иначе обычный ssh по ключу
# (REMOTE_SSH_KEY / ssh-agent) — основной способ для QNAP, т.к. в Entware для
# этой архитектуры нет пакета sshpass.
ssh_remote() {
  if [ -n "$REMOTE_PASSWORD" ] && command -v sshpass >/dev/null 2>&1; then
    SSHPASS="$REMOTE_PASSWORD" sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "$1"
  else
    ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "$1"
  fi
}

# Выбираем быстрейший доступный компрессор на удалённом сервере:
# zstd --fast=1 --threads=0 > pigz -1 > gzip -1
REMOTE_COMP=$(ssh_remote \
  "if command -v zstd >/dev/null 2>&1; then echo zstd; elif command -v pigz >/dev/null 2>&1; then echo pigz; else echo gzip; fi" 2>/dev/null)
case "$REMOTE_COMP" in
  zstd) COMP_CMD="zstd --fast=1 --threads=0 -c"; BACKUP_EXT="tar.zst" ;;
  pigz) COMP_CMD="pigz -1";                       BACKUP_EXT="tar.gz"  ;;
  *)    COMP_CMD="gzip -1";                        BACKUP_EXT="tar.gz"  ;;
esac

BACKUP_FILENAME="${SCRIPT_BASE}-${BACKUP_DATE}.${BACKUP_EXT}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILENAME}"

if [ -f "$BACKUP_PATH" ]; then
  if validate_backup_file "$BACKUP_PATH"; then
    BACKUP_APPEND=1
    log_json "INFO" "backup_append_mode" "Файл бэкапа за ${BACKUP_DATE} уже существует и прошёл проверку — дозапись" \
      "file=${BACKUP_PATH}"
  else
    validate_rc=$?
    BACKUP_APPEND=0
    validate_detail="file=${BACKUP_PATH}"
    if [ -n "$VALIDATE_BACKUP_DETAIL" ]; then
      validate_detail="${validate_detail}, reason=${VALIDATE_BACKUP_DETAIL}"
    fi
    if [ $validate_rc -eq 1 ]; then
      log_json "WARN" "backup_existing_invalid" "Существующий файл бэкапа повреждён — удаляем и создаём заново" \
        "$validate_detail" $validate_rc
      rm -f "$BACKUP_PATH"
    else
      log_json "WARN" "backup_existing_unchecked" "Не удалось проверить существующий файл бэкапа — создаём заново без дозаписи" \
        "$validate_detail" $validate_rc
    fi
  fi
else
  BACKUP_APPEND=0
fi

log_json "INFO" "backup_start" "Начало резервного копирования" \
  "remote=${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_PATH} -> ${BACKUP_PATH}, comp=${REMOTE_COMP:-gzip}, append=${BACKUP_APPEND}"
log_json "INFO" "backup_excludes" "Применены встроенные исключения tar" "count=${#REMOTE_EXCLUDES[@]}, list=${REMOTE_EXCLUDES[*]}"

###############################################################################
# NEXTCLOUD CLEANUP (occ)
###############################################################################
log_json "INFO" "occ_cleanup_start" "Очистка корзин пользователей (occ trashbin:cleanup)..."
occ_trash_err=$(ssh_remote \
  "docker exec -u www-data esimych-cloud-app php occ trashbin:cleanup --all-users" 2>&1)
occ_trash_rc=$?
if [ $occ_trash_rc -ne 0 ]; then
  log_json "WARN" "occ_cleanup_trash_failed" "Не удалось очистить корзины" "$occ_trash_err" $occ_trash_rc
else
  log_json "INFO" "occ_cleanup_trash_ok" "Корзины очищены" "$occ_trash_err" $occ_trash_rc
fi

# "occ trashbin:cleanup --all-users" при полной очистке физически удаляет
# саму папку data/<user>/files_trashbin (не только её содержимое), если она
# опустела. Из-за этого встроенный фоновый джоб Nextcloud ExpireTrash (он
# запускается через cron.php независимо от расписания этого скрипта) затем
# падает с "NotFoundException.../files_trashbin" при каждом своём запуске,
# пока папка не появится снова. Пересоздаём её для каждого пользователя сразу
# после очистки — mkdir -p идемпотентен и безопасен.
occ_trash_repair_detail=""
occ_trash_repair_rc=0
remote_datadir=$(ssh_remote \
  "docker exec -u www-data esimych-cloud-app php occ config:system:get datadirectory" 2>/dev/null | tr -d '\r\n')
remote_users=$(ssh_remote \
  "docker exec -u www-data esimych-cloud-app php occ user:list" 2>/dev/null)
log_json "INFO" "occ_user_list" "Получен список пользователей Nextcloud (occ user:list)" "${remote_users:-<пусто>}"
if [ -z "$remote_datadir" ] || [ -z "$remote_users" ]; then
  occ_trash_repair_rc=1
  occ_trash_repair_detail="не удалось получить datadirectory или список пользователей"
else
  # Читаем список пользователей из отдельного файлового дескриптора (3), а не
  # из stdin (0): ssh_remote() внутри тела цикла вызывает ssh без "-n", и он
  # по умолчанию читает stdin. Если список подавать через <<< на стандартный
  # ввод цикла, первый же вызов ssh внутри тела "съедает" из stdin остаток
  # списка пользователей, из-за чего while read получает EOF и обрабатывает
  # только первого пользователя — остальные (например, clouduser) молча
  # пропускаются без пересоздания files_trashbin и без записи в лог.
  while IFS= read -r _uid <&3; do
    [ -z "$_uid" ] && continue
    _repair_err=$(ssh_remote \
      "docker exec -u www-data esimych-cloud-app mkdir -p '${remote_datadir}/${_uid}/files_trashbin'" 2>&1)
    _repair_rc=$?
    if [ $_repair_rc -ne 0 ]; then
      occ_trash_repair_rc=1
      occ_trash_repair_detail="${occ_trash_repair_detail}${_uid}: ${_repair_err}; "
      continue
    fi
    # Проверяем, что папка реально существует после mkdir -p (а не просто
    # команда молча ничего не сделала из-за проблем с docker exec/SSH) —
    # результат логируется отдельно для каждого пользователя.
    ssh_remote "docker exec -u www-data esimych-cloud-app test -d '${remote_datadir}/${_uid}/files_trashbin'" >/dev/null 2>&1
    _verify_rc=$?
    if [ $_verify_rc -eq 0 ]; then
      log_json "INFO" "occ_trashbin_verify_ok" "Папка files_trashbin подтверждена после пересоздания" "user=${_uid}, path=${remote_datadir}/${_uid}/files_trashbin" 0
    else
      occ_trash_repair_rc=1
      occ_trash_repair_detail="${occ_trash_repair_detail}${_uid}: папка не найдена после mkdir -p; "
      log_json "ERROR" "occ_trashbin_verify_failed" "Папка files_trashbin отсутствует после попытки пересоздания" "user=${_uid}, path=${remote_datadir}/${_uid}/files_trashbin" $_verify_rc
    fi
  done 3<<< "$(printf '%s\n' "$remote_users" | sed -nE 's/^[[:space:]]*-[[:space:]]*([^:]+):.*/\1/p')"
fi
if [ $occ_trash_repair_rc -ne 0 ]; then
  log_json "WARN" "occ_trashbin_repair_failed" "Не удалось пересоздать/подтвердить папки files_trashbin" "$occ_trash_repair_detail" $occ_trash_repair_rc
else
  log_json "INFO" "occ_trashbin_repair_ok" "Папки files_trashbin пересозданы и проверены для всех пользователей" "" 0
fi

log_json "INFO" "occ_versions_start" "Очистка версий файлов (occ versions:cleanup)..."
occ_ver_err=$(ssh_remote \
  "docker exec -u www-data esimych-cloud-app php occ versions:cleanup" 2>&1)
occ_ver_rc=$?
if [ $occ_ver_rc -ne 0 ]; then
  log_json "WARN" "occ_cleanup_versions_failed" "Не удалось очистить версии файлов" "$occ_ver_err" $occ_ver_rc
else
  log_json "INFO" "occ_cleanup_versions_ok" "Версии файлов очищены" "$occ_ver_err" $occ_ver_rc
fi

###############################################################################
# DATABASE OPTIMIZATION (before services down)
###############################################################################
if [ "$OPTIMIZE_MARIADB_BEFORE_BACKUP" -eq 1 ]; then
  log_json "INFO" "mariadb_optimize_start" "Оптимизация MariaDB перед архивированием"
  mariadb_opt_err=$(ssh_remote \
    "cd '${REMOTE_PATH}' && docker compose exec -T \
      -e PURGE_BINLOGS='${MARIADB_PURGE_BINLOGS}' \
      -e TRUNCATE_GENERAL_LOG='${MARIADB_TRUNCATE_GENERAL_LOG}' \
      '${MARIADB_SERVICE_NAME}' sh -lc 'set -e; \
        mariadb-check -uroot -p\"\$MARIADB_ROOT_PASSWORD\" --optimize --all-databases --skip-database=information_schema --skip-database=performance_schema --skip-database=mysql --skip-database=sys; \
        if [ \"\$PURGE_BINLOGS\" = \"1\" ]; then \
          mariadb -uroot -p\"\$MARIADB_ROOT_PASSWORD\" -e \"PURGE BINARY LOGS BEFORE NOW();\"; \
        fi; \
        if [ \"\$TRUNCATE_GENERAL_LOG\" = \"1\" ]; then \
          : > /var/lib/mysql/general.log || true; \
        fi'" 2>&1)
  mariadb_opt_rc=$?
  if [ $mariadb_opt_rc -ne 0 ]; then
    log_json "WARN" "mariadb_optimize_failed" "Оптимизация MariaDB завершилась с предупреждениями" "$mariadb_opt_err" $mariadb_opt_rc
  else
    log_json "INFO" "mariadb_optimize_ok" "Оптимизация MariaDB завершена" "$mariadb_opt_err" $mariadb_opt_rc
  fi
else
  log_json "INFO" "mariadb_optimize_skip" "Оптимизация MariaDB отключена (OPTIMIZE_MARIADB_BEFORE_BACKUP=0)"
fi

if [ "$OPTIMIZE_REDIS_BEFORE_BACKUP" -eq 1 ]; then
  log_json "INFO" "redis_optimize_start" "Оптимизация Redis AOF перед архивированием"
  redis_opt_err=$(ssh_remote \
    "cd '${REMOTE_PATH}' && docker compose exec -T \
      -e REDIS_WAIT='${REDIS_REWRITE_WAIT_SEC}' \
      '${REDIS_SERVICE_NAME}' sh -lc 'set -e; \
        redis-cli BGREWRITEAOF >/dev/null; \
        i=0; \
        while [ \$i -lt \$REDIS_WAIT ]; do \
          in_progress=\$(redis-cli INFO persistence | sed -n \"s/^aof_rewrite_in_progress:\\([0-9]\\+\\)$/\\1/p\"); \
          [ \"\$in_progress\" = \"0\" ] && exit 0; \
          i=\$((i + 1)); \
          sleep 1; \
        done; \
        echo \"AOF rewrite did not finish within \$REDIS_WAIT seconds\"; \
        exit 1'" 2>&1)
  redis_opt_rc=$?
  if [ $redis_opt_rc -ne 0 ]; then
    log_json "WARN" "redis_optimize_failed" "Оптимизация Redis завершилась с предупреждениями" "$redis_opt_err" $redis_opt_rc
  else
    log_json "INFO" "redis_optimize_ok" "Оптимизация Redis завершена" "$redis_opt_err" $redis_opt_rc
  fi
else
  log_json "INFO" "redis_optimize_skip" "Оптимизация Redis отключена (OPTIMIZE_REDIS_BEFORE_BACKUP=0)"
fi

log_json "INFO" "services_stop" "Останавливаем сервисы на ${REMOTE_HOST}..."
stop_err=$(ssh_remote \
  "cd '${REMOTE_PATH}' && docker compose down" 2>&1)
stop_rc=$?
if [ $stop_rc -ne 0 ]; then
  log_json "WARN" "services_stop_failed" "Не удалось остановить сервисы" "$stop_err" $stop_rc
  echo "Предупреждение: не удалось остановить сервисы (код $stop_rc): $stop_err" >&2
else
  SERVICES_STOPPED=1
  log_json "INFO" "services_stop_ok" "Сервисы остановлены" "" $stop_rc
fi

###############################################################################
# SQLITE OPTIMIZATION (optional)
###############################################################################
if [ "$OPTIMIZE_SQLITE_BEFORE_BACKUP" -eq 1 ]; then
  log_json "INFO" "sqlite_optimize_start" "Оптимизация SQLite-баз перед архивированием"
  sqlite_opt_err=$(ssh_remote \
    "set -o pipefail; find '${REMOTE_PATH}' -type f -name '*.db' -print0 \
      | xargs -0 -r -I{} timeout ${SQLITE_OPTIMIZE_TIMEOUT_SEC}s sqlite3 \"{}\" 'PRAGMA optimize; VACUUM;'" 2>&1)
  sqlite_opt_rc=$?
  if [ $sqlite_opt_rc -ne 0 ]; then
    log_json "WARN" "sqlite_optimize_failed" "Оптимизация SQLite завершилась с предупреждениями" "$sqlite_opt_err" $sqlite_opt_rc
  else
    log_json "INFO" "sqlite_optimize_ok" "Оптимизация SQLite завершена" "$sqlite_opt_err" $sqlite_opt_rc
  fi
else
  log_json "INFO" "sqlite_optimize_skip" "Оптимизация SQLite отключена (OPTIMIZE_SQLITE_BEFORE_BACKUP=0)"
fi

PROGRESS_METRICS_FILE="$(mktemp)"

# Фоновый монитор: пишет размер файла в лог каждые 60 сек
_progress_monitor() {
  while sleep 60; do
    [ -f "$BACKUP_PATH" ] || break
    local sz size_bytes now_epoch
    sz=$(du -sh "$BACKUP_PATH" 2>/dev/null | cut -f1)
    size_bytes=$(stat -c%s "$BACKUP_PATH" 2>/dev/null || wc -c < "$BACKUP_PATH" 2>/dev/null || printf '0')
    now_epoch=$(date +%s)
    printf '%s\t%s\n' "$now_epoch" "$size_bytes" >> "$PROGRESS_METRICS_FILE"
    [ -n "$sz" ] && log_json "INFO" "backup_progress" "Прогресс архивирования" "size=${sz}, bytes=${size_bytes}"
  done
}
_progress_monitor >/dev/null 2>&1 &
_PROGRESS_PID=$!

ARCHIVE_START_EPOCH=$(date +%s)
err_tmp=$(mktemp)
REMOTE_TAR_CMD="set -o pipefail; tar --create --file=- --sparse${REMOTE_TAR_EXCLUDE_ARGS} --directory='${REMOTE_PARENT}' '${REMOTE_DIR}' | ${COMP_CMD}"
if [ "$BACKUP_APPEND" -eq 1 ]; then
  ssh_remote "$REMOTE_TAR_CMD" \
    >> "$BACKUP_PATH" 2>"$err_tmp"
else
  ssh_remote "$REMOTE_TAR_CMD" \
    > "$BACKUP_PATH" 2>"$err_tmp"
fi
backup_rc=$?
err_out=$(cat "$err_tmp"); rm -f "$err_tmp"
ARCHIVE_END_EPOCH=$(date +%s)

kill "$_PROGRESS_PID" 2>/dev/null
wait "$_PROGRESS_PID" 2>/dev/null

if [ $backup_rc -eq 0 ]; then
  archive_duration=$((ARCHIVE_END_EPOCH - ARCHIVE_START_EPOCH))
  [ "$archive_duration" -lt 1 ] && archive_duration=1

  backup_bytes=$(stat -c%s "$BACKUP_PATH" 2>/dev/null || wc -c < "$BACKUP_PATH" 2>/dev/null || printf '0')
  backup_size=$(du -sh "$BACKUP_PATH" 2>/dev/null | cut -f1)
  avg_mib_s=$(awk -v b="$backup_bytes" -v d="$archive_duration" 'BEGIN { printf "%.2f", (b/1048576)/d }')

  progress_samples=$(wc -l < "$PROGRESS_METRICS_FILE" 2>/dev/null || printf '0')
  window_mib_s="n/a"
  if [ "$progress_samples" -ge 2 ]; then
    first_sample=$(head -n1 "$PROGRESS_METRICS_FILE")
    last_sample=$(tail -n1 "$PROGRESS_METRICS_FILE")
    first_ts=$(printf '%s' "$first_sample" | awk -F '\t' '{print $1}')
    first_bytes=$(printf '%s' "$first_sample" | awk -F '\t' '{print $2}')
    last_ts=$(printf '%s' "$last_sample" | awk -F '\t' '{print $1}')
    last_bytes=$(printf '%s' "$last_sample" | awk -F '\t' '{print $2}')
    window_dt=$((last_ts - first_ts))
    if [ "$window_dt" -gt 0 ]; then
      window_mib_s=$(awk -v b1="$first_bytes" -v b2="$last_bytes" -v d="$window_dt" 'BEGIN { printf "%.2f", ((b2-b1)/1048576)/d }')
    fi
  fi

  metrics_detail="file=${BACKUP_PATH}, size=${backup_size}, bytes=${backup_bytes}, duration_s=${archive_duration}, avg_mib_s=${avg_mib_s}, window_mib_s=${window_mib_s}, progress_samples=${progress_samples}, comp=${REMOTE_COMP:-gzip}, append=${BACKUP_APPEND}"
  log_json "INFO" "backup_metrics" "Метрики этапа архивирования" "$metrics_detail" 0

  if awk -v s="$avg_mib_s" -v t="$BACKUP_DEGRADATION_MIBS_THRESHOLD" 'BEGIN { exit !(s < t) }'; then
    probable_cause="network_or_remote_io"
    if [ "${REMOTE_COMP:-gzip}" = "zstd" ]; then
      probable_cause="network_or_remote_io_or_zstd_cpu"
    fi
    if [ "$progress_samples" -lt 2 ]; then
      probable_cause="insufficient_progress_samples"
    fi
    log_json "WARN" "backup_degradation" "Обнаружена деградация скорости бэкапа" \
      "threshold_mib_s=${BACKUP_DEGRADATION_MIBS_THRESHOLD}, probable_cause=${probable_cause}, ${metrics_detail}" 0
  fi

  backup_size=$(du -sh "$BACKUP_PATH" 2>/dev/null | cut -f1)
  log_json "INFO" "backup_done" "Резервная копия создана успешно" \
    "file=${BACKUP_PATH}, size=${backup_size}" $backup_rc
  echo "Готово: ${BACKUP_PATH} (${backup_size})"

  # Очистка старых бэкапов, оставляем только 5 последних
  old_backups=$(ls -1t "${BACKUP_DIR}/${SCRIPT_BASE}-"*.tar.* 2>/dev/null | tail -n +6)
  if [ -n "$old_backups" ]; then
    log_json "INFO" "backup_cleanup" "Удаление старых резервных копий (оставляем 5)"
    echo "$old_backups" | tr '\n' '\0' | xargs -0 -r rm -f
  fi
else
  log_json "ERROR" "backup_failed" "Ошибка при создании резервной копии" "$err_out" $backup_rc
  echo "Ошибка резервного копирования (код $backup_rc): $err_out" >&2
  rm -f "$BACKUP_PATH" 2>/dev/null
  exit $backup_rc
fi

rm -f "$PROGRESS_METRICS_FILE" 2>/dev/null

exit 0
