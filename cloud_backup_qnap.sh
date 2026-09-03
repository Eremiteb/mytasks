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
#     содержит wg-quick, wg, zstd и т.п.;
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
#        opkg install bash wireguard-tools wireguard-go ncat xxd zstd coreutils-stat
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
#      Внешний endpoint также можно переопределить через WG_ENDPOINT в
#      conf/cloud_backup_qnap.conf — это удобнее при смене публичного IP.
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
#   8. (Опционально, только если включаете RAW_TRANSFER_ENABLED="1" в conf —
#      передача потока бэкапа в обход SSH-шифрования, см. комментарий у
#      RAW_TRANSFER_ENABLED ниже) Один раз открыть порт RAW_TRANSFER_PORT
#      (по умолчанию 8873/tcp) в фаерволе удалённого сервера, ограничив его
#      той же подсетью, что и SSH. На этом сервере `ufw` управляется не с
#      хоста, а из контейнера esimych-cloud-fail2ban (см. docker-compose.yml,
#      network_mode: host + cap_add NET_ADMIN, том ufw_data:/etc/ufw) —
#      выполнить на самом REMOTE_HOST:
#        docker exec esimych-cloud-fail2ban ufw allow from 10.66.66.0/24 to any port 8873 proto tcp comment 'raw backup transfer (10.66.66.0/24)'
#      Проверить: docker exec esimych-cloud-fail2ban ufw show added
#      (у "ufw status" на этом сервере известная безобидная ошибка "problem
#      running sysctl" — на добавление/применение правил не влияет).
###############################################################################

set -uo pipefail

# Entware-утилиты (wg-quick, wg, zstd и т.д.) лежат в /opt/*bin,
# которых обычно нет в PATH при запуске из Task Scheduler/cron.
export PATH="/opt/sbin:/opt/bin:${PATH}"

# Каталог с постоянными данными Entware на диске (см. шаг 1 выше). Если /opt
# ещё не примонтирован (например, после перезагрузки NAS, а отдельной задачи
# "на загрузку" в Планировщике нет), переподключаем его здесь же — тогда
# для работы скрипта достаточно, чтобы ОН САМ запускался по расписанию.
ENTWARE_DATA_DIR="${ENTWARE_DATA_DIR:-/share/Public/entware}"
ensure_entware_mount() {
  [[ -x /opt/bin/opkg ]] && return 0
  [[ -d "${ENTWARE_DATA_DIR}/etc" ]] || return 0
  mkdir -p /opt
  mount --bind "${ENTWARE_DATA_DIR}" /opt 2>/dev/null
  [[ -x /opt/etc/init.d/rc.unslung ]] && /opt/etc/init.d/rc.unslung start >/dev/null 2>&1
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
DIAG_METRICS_FILE=""
NETWORK_SPEED_TEST_MIB_S="n/a"

mkdir -p "${LOG_DIR}"

if [[ -r "${LOG_TEMPLATE_FILE}" ]]; then
  # shellcheck source=/dev/null
  source "${LOG_TEMPLATE_FILE}"
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
    | tr -d '\001\002\003\004\005\006\007\010\013\014\016\017\020\021\022\023\024\025\026\027\030\031\032\033\034\035\036\037' \
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
  msg_esc="$(json_escape "${msg}")"
  detail_esc="$(json_escape "${detail}")"
  level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
  printf '{"@timestamp":"%s","ts":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s","rc":%s}\n' \
    "${ts_val}" "${ts_val}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" "${rc}" >> "${LOG_FILE}"
}

# shellcheck disable=SC2329
cleanup_logs() {
  local old_logs
  # shellcheck disable=SC2012
  old_logs=$(ls -1t "${LOG_DIR}/${SCRIPT_BASE}-"*.jsonl 2>/dev/null | tail -n "+$((BACKUP_KEEP_COUNT + 1))")
  if [[ -n "${old_logs}" ]]; then
    printf '%s\n' "${old_logs}" | xargs -r rm -f --
  fi
}

VALIDATE_BACKUP_DETAIL=""

validate_backup_file() {
  local backup_path="${1}"
  local validate_out rc

  VALIDATE_BACKUP_DETAIL=""

  if [[ ! -s "${backup_path}" ]]; then
    VALIDATE_BACKUP_DETAIL="file is empty"
    return 1
  fi

  case "${backup_path}" in
    *.tar.gz)
      if ! command -v gzip >/dev/null 2>&1; then
        VALIDATE_BACKUP_DETAIL="gzip not found"
        return 2
      fi
      validate_out=$(gzip -t "${backup_path}" 2>&1)
      rc=$?
      VALIDATE_BACKUP_DETAIL="${validate_out}"
      return "${rc}"
      ;;
    *.tar.zst)
      if ! command -v zstd >/dev/null 2>&1; then
        VALIDATE_BACKUP_DETAIL="zstd not found (opkg install zstd)"
        return 2
      fi
      validate_out=$(zstd -t "${backup_path}" 2>&1)
      rc=$?
      VALIDATE_BACKUP_DETAIL="${validate_out}"
      return "${rc}"
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
if [[ ! -r "${CONFIG_FILE}" ]]; then
  echo "Ошибка: конфигурационный файл не найден: ${CONFIG_FILE}" >&2
  exit 1
fi

# shellcheck source=/dev/null
source "${CONFIG_FILE}"

# Значения приходят из source "${CONFIG_FILE}", но часть IDE-анализаторов не
# умеет отслеживать такие присваивания и помечает переменные как "не заданы".
# Явно инициализируем пустыми значениями, затем ниже всё равно проверяем
# обязательность через цикл for var in ...
REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_USER="${REMOTE_USER:-}"
BACKUP_DIR="${BACKUP_DIR:-}"

for var in WG_INTERFACE REMOTE_HOST REMOTE_USER BACKUP_DIR; do
  if [[ -z "${!var:-}" ]]; then
    echo "Ошибка: переменная ${var} не задана в ${CONFIG_FILE}" >&2
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
WG_ENDPOINT="${WG_ENDPOINT:-}"
BACKUP_KEEP_COUNT="${BACKUP_KEEP_COUNT:-5}"
BACKUP_DEGRADATION_MIBS_THRESHOLD="${BACKUP_DEGRADATION_MIBS_THRESHOLD:-4}"
NEXTCLOUD_SERVICE_NAME="${NEXTCLOUD_SERVICE_NAME:-nextcloud}"
OPTIMIZE_MARIADB_BEFORE_BACKUP="${OPTIMIZE_MARIADB_BEFORE_BACKUP:-0}"
MARIADB_SERVICE_NAME="${MARIADB_SERVICE_NAME:-mariadb}"
MARIADB_PURGE_BINLOGS="${MARIADB_PURGE_BINLOGS:-0}"
MARIADB_TRUNCATE_GENERAL_LOG="${MARIADB_TRUNCATE_GENERAL_LOG:-1}"
OPTIMIZE_REDIS_BEFORE_BACKUP="${OPTIMIZE_REDIS_BEFORE_BACKUP:-0}"
REDIS_SERVICE_NAME="${REDIS_SERVICE_NAME:-valkey}"
REDIS_REWRITE_WAIT_SEC="${REDIS_REWRITE_WAIT_SEC:-180}"
REDIS_LOG_TAIL_LINES="${REDIS_LOG_TAIL_LINES:-300}"
SERVICES_START_RETRIES="${SERVICES_START_RETRIES:-3}"
SERVICES_START_RETRY_DELAY_SEC="${SERVICES_START_RETRY_DELAY_SEC:-15}"

# Передача потока бэкапа через сырой TCP-сокет (nc/ncat) внутри уже
# зашифрованного WireGuard-туннеля, минуя дополнительный слой SSH-шифрования —
# см. обоснование у SSH_CIPHERS ниже (двойное шифрование на единственном ядре
# CPU QNAP). Конфиденциальность/целостность не теряются — их обеспечивает AEAD
# самого WireGuard; SSH по-прежнему используется для служебных команд
# (occ cleanup, optimize БД, docker compose down/up) и для запуска/остановки
# самого TCP-листенера на удалённой стороне.
# По умолчанию выключено (0) — это единственный feature-флаг в этом скрипте,
# сделан намеренно: новый путь передачи непроверен на проде, откат должен
# быть мгновенным (без редеплоя) на случай проблем в необслуживаемом ночном
# запуске. Включить после проверки: RAW_TRANSFER_ENABLED="1" в conf.
RAW_TRANSFER_ENABLED="${RAW_TRANSFER_ENABLED:-0}"
RAW_TRANSFER_PORT="${RAW_TRANSFER_PORT:-8873}"
RAW_TRANSFER_CONNECT_RETRIES="${RAW_TRANSFER_CONNECT_RETRIES:-10}"
RAW_TRANSFER_REMOTE_TIMEOUT_SEC="${RAW_TRANSFER_REMOTE_TIMEOUT_SEC:-21600}"
RAW_TRANSFER_STATUS_POLL_RETRIES="${RAW_TRANSFER_STATUS_POLL_RETRIES:-5}"

# Замер реальной скорости сети QNAP<->REMOTE_HOST перед началом бэкапа —
# тем же способом (nc -N + ncat --recv-only), что и сама передача, поэтому
# число напрямую сравнимо с avg_mib_s в backup_metrics: если замер и реальная
# скорость близки — узкое место на стороне QNAP (CPU/диск), если замер
# гораздо выше реальной скорости — узкое место где-то в процессе архивации
# (remote CPU/IO), а не в самой сети. См. анализ логов 07-30..08-03: скорость
# самого бэкапа скакала от 2.51 до 5.01 МиБ/с без явной корреляции с
# local_load1 — этот замер даёт третью точку данных для разбора таких случаев.
NETWORK_SPEED_TEST_MB="${NETWORK_SPEED_TEST_MB:-50}"

# Раз в сколько дней реально выполнять оптимизацию БД (MariaDB/Redis),
# даже если сам бэкап запускается ежедневно. Обоснование по логам за
# 2026-07-24..29: узкое место скорости архивирования — локальный CPU QNAP
# (nproc=1, avg_load1 во время передачи ~1.5-1.8, см. probable_cause в
# backup_degradation), а не состояние удалённых БД, т.е. ежедневная
# оптимизация НЕ помогает со скоростью бэкапа. При этом сама оптимизация не
# бесплатна: MariaDB для части таблиц вместо OPTIMIZE делает recreate+analyze
# (полная перезапись таблицы), а Redis BGREWRITEAOF 2026-07-29 не уложился в
# REDIS_REWRITE_WAIT_SEC=600 и провалился — т.е. ежедневно тратится время
# впустую. Раз в неделю достаточно для типичной нагрузки Nextcloud.
DB_OPTIMIZE_INTERVAL_DAYS="${DB_OPTIMIZE_INTERVAL_DAYS:-7}"
STATE_DIR="${SCRIPT_DIR}/state"
DB_OPTIMIZE_STATE_FILE="${STATE_DIR}/${SCRIPT_BASE}-db-optimize.state"

# true (0), если с последней оптимизации БД прошло >= DB_OPTIMIZE_INTERVAL_DAYS
# дней, либо оптимизация ещё ни разу не запускалась (нет state-файла/битое
# содержимое) — в этом случае тоже считаем, что пора.
db_optimize_due() {
  local last_epoch now_epoch elapsed_days
  [[ -r "${DB_OPTIMIZE_STATE_FILE}" ]] || return 0
  last_epoch=$(cat "${DB_OPTIMIZE_STATE_FILE}" 2>/dev/null)
  case "${last_epoch}" in
    ''|*[!0-9]*) return 0 ;;
    *) ;;
  esac
  now_epoch=$(date +%s)
  elapsed_days=$(( (now_epoch - last_epoch) / 86400 ))
  [[ "${elapsed_days}" -ge "${DB_OPTIMIZE_INTERVAL_DAYS}" ]]
}

db_optimize_mark_done() {
  mkdir -p "${STATE_DIR}"
  date +%s > "${DB_OPTIMIZE_STATE_FILE}"
}

if db_optimize_due; then
  DB_OPTIMIZE_DUE=1
  log_json "INFO" "db_optimize_due" "Плановая оптимизация БД (раз в ${DB_OPTIMIZE_INTERVAL_DAYS} дн.) выполняется в этом запуске"
else
  DB_OPTIMIZE_DUE=0
  log_json "INFO" "db_optimize_not_due" "Оптимизация БД пропущена в этом запуске — интервал ${DB_OPTIMIZE_INTERVAL_DAYS} дн. ещё не истёк" "state_file=${DB_OPTIMIZE_STATE_FILE}"
fi

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
# WG_SOCK_OVERRIDE существует только для тестов (bats не может создавать
# сокет-файлы под /var/run без root) — в проде переменная не задаётся, путь
# всегда фактический.
WG_SOCK="${WG_SOCK_OVERRIDE:-/var/run/wireguard/${WG_INTERFACE:-wg0-qnap}.sock}"
WG_BROUGHT_UP=0
SERVICES_STOPPED=0

# Извлекает значение "Key = value" из конфига WireGuard (регистр ключа не
# важен: PrivateKey/privatekey и т.п. распознаются одинаково).
wg_conf_get() {
  local key_lc
  key_lc=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  awk -F= -v k="${key_lc}" '
    {
      line = $0; keypart = $1
      gsub(/^[ \t]+|[ \t]+$/, "", keypart)
      if (tolower(keypart) == k) {
        sub(/^[^=]*=/, "", line)
        gsub(/^[ \t]+|[ \t]+$/, "", line)
        print line
        exit
      }
    }' "${WG_CONF_FILE}"
}

# Конвертирует base64-ключ WireGuard в hex, как того требует UAPI-протокол
# (https://www.wireguard.com/xplatform/).
wg_b64_to_hex() {
  printf '%s' "$1" | base64 -d 2>/dev/null | xxd -p -c 256 | tr -d '\n'
}

# true, если туннель уже поднят (сокет есть и интерфейс в состоянии UP) —
# замена сломанного "wg show" (см. ВАЖНО выше).
wg_is_up() {
  [[ -S "${WG_SOCK}" ]] || return 1
  ip link show "${WG_INTERFACE}" 2>/dev/null | grep -q "state UP" || return 1
  return 0
}

# Поднимает WireGuard в обход wg/wg-quick: запускает wireguard-go, настраивает
# пира через сырой UAPI поверх ncat -U и назначает адрес/маршрут штатной ip(8).
wg_up_userspace() {
  local priv_b64 addr peer_pub_b64 psk_b64 endpoint endpoint_source keepalive psk_hex
  local priv_hex peer_pub_hex uapi_tmp set_out i

  [[ -r "${WG_CONF_FILE}" ]] || {
    log_json "ERROR" "wg_conf_missing" "Конфиг WireGuard не найден" "${WG_CONF_FILE}"
    return 1
  }

  priv_b64=$(wg_conf_get PrivateKey)
  addr=$(wg_conf_get Address)
  peer_pub_b64=$(wg_conf_get PublicKey)
  psk_b64=$(wg_conf_get PresharedKey)
  endpoint=$(wg_conf_get Endpoint)
  endpoint_source="wireguard_config"
  if [[ -n "${WG_ENDPOINT}" ]]; then
    endpoint="${WG_ENDPOINT}"
    endpoint_source="backup_config"
  fi
  keepalive=$(wg_conf_get PersistentKeepalive)
  keepalive="${keepalive:-25}"

  if [[ -z "${priv_b64}" || -z "${addr}" || -z "${peer_pub_b64}" || -z "${endpoint}" ]]; then
    log_json "ERROR" "wg_conf_incomplete" "В конфиге отсутствуют обязательные поля" \
      "нужны PrivateKey, Address, PublicKey и Endpoint в ${WG_CONF_FILE} либо WG_ENDPOINT в ${CONFIG_FILE}"
    return 1
  fi

  log_json "INFO" "wg_endpoint_selected" "Выбран endpoint WireGuard" \
    "endpoint=${endpoint}, source=${endpoint_source}"

  priv_hex=$(wg_b64_to_hex "${priv_b64}")
  peer_pub_hex=$(wg_b64_to_hex "${peer_pub_b64}")

  rm -f "${WG_SOCK}" 2>/dev/null
  ip link delete "${WG_INTERFACE}" 2>/dev/null

  WG_I_PREFER_BUGGY_USERSPACE_TO_POLISHED_KMOD=1 wireguard-go "${WG_INTERFACE}" >/dev/null 2>&1
  psk_hex=""
  if [[ -n "${psk_b64}" ]]; then
    psk_hex=$(wg_b64_to_hex "${psk_b64}")
  fi

  i=0
  while [[ ! -S "${WG_SOCK}" && "${i}" -lt 25 ]]; do
    sleep 0.2
    i=$((i + 1))
  done
  if [[ ! -S "${WG_SOCK}" ]]; then
    log_json "ERROR" "wireguard_go_no_socket" "wireguard-go не создал UAPI-сокет" "${WG_SOCK}"
    return 1
  fi

  uapi_tmp="$(mktemp)"
  {
    echo "set=1"
    echo "private_key=${priv_hex}"
    echo "listen_port=0"
    echo "replace_peers=true"
    echo "public_key=${peer_pub_hex}"
    [[ -n "${psk_hex}" ]] && echo "preshared_key=${psk_hex}"
    echo "endpoint=${endpoint}"
    echo "persistent_keepalive_interval=${keepalive}"
    echo "replace_allowed_ips=true"
    echo "allowed_ip=${REMOTE_HOST}/32"
    echo ""
  } > "${uapi_tmp}"
  set_out=$(ncat -U "${WG_SOCK}" < "${uapi_tmp}" 2>&1)
  rm -f "${uapi_tmp}"

  if ! printf '%s' "${set_out}" | grep -q '^errno=0'; then
    log_json "ERROR" "wg_uapi_set_failed" "UAPI 'set' вернул ошибку" "${set_out}"
    return 1
  fi

  ip address add "${addr}" dev "${WG_INTERFACE}" 2>/dev/null
  ip link set mtu 1420 up dev "${WG_INTERFACE}" 2>&1
  ip route replace "${REMOTE_HOST}/32" dev "${WG_INTERFACE}" 2>&1

  return 0
}

# Останавливает туннель: завершает wireguard-go (интерфейс и сокет исчезают
# вместе с процессом), подчищает сокет-файл, если он вдруг остался.
# shellcheck disable=SC2329
wg_down_userspace() {
  local pid
  # shellcheck disable=SC2009
  pid=$(ps w 2>/dev/null | grep "[w]ireguard-go ${WG_INTERFACE}" | awk '{print $1}')
  [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null
  rm -f "${WG_SOCK}" 2>/dev/null
  return 0
}

# shellcheck disable=SC2329
wg_down_if_needed() {
  if [[ "${WG_BROUGHT_UP}" -eq 1 && "${WG_KEEP_UP:-0}" -ne 1 ]]; then
    log_json "INFO" "wg_down" "Останавливаем WireGuard ${WG_INTERFACE}..."
    if wg_down_userspace; then
      log_json "INFO" "wg_down_ok" "WireGuard ${WG_INTERFACE} остановлен"
    else
      log_json "WARN" "wg_down_fail" "Не удалось остановить WireGuard ${WG_INTERFACE}"
    fi
  fi
}

# shellcheck disable=SC2329
services_start_if_needed() {
  if [[ "${SERVICES_STOPPED}" -eq 1 ]]; then
    local attempt start_out start_rc services_state services_state_rc recovery_diag
    attempt=1
    while [[ "${attempt}" -le "${SERVICES_START_RETRIES}" ]]; do
      log_json "INFO" "services_start" "Запускаем сервисы на ${REMOTE_HOST}..." \
        "attempt=${attempt}, max_attempts=${SERVICES_START_RETRIES}"
      start_out=$(ssh_remote \
        "cd '${REMOTE_PATH}' && docker compose up -d" 2>&1)
      start_rc=$?
      services_state=""
      services_state_rc=1
      if [[ "${start_rc}" -eq 0 ]]; then
        services_state=$(ssh_remote \
          "cd '${REMOTE_PATH}' && docker compose ps -a" 2>&1)
        services_state_rc=$?
        if [[ "${services_state_rc}" -eq 0 ]] \
            && ! printf '%s\n' "${services_state}" | grep -Eiq 'unhealthy|exited|dead|restarting'; then
          SERVICES_STOPPED=0
          log_json "INFO" "services_start_ok" "Сервисы запущены" \
            "attempt=${attempt}; compose_ps=${services_state:-<пусто>}" 0
          return 0
        fi
        start_rc=1
        start_out="${start_out}; docker compose ps -a: ${services_state:-<пусто>}"
      fi

      log_json "WARN" "services_start_retry" "Не удалось запустить все сервисы — повторяем попытку" \
        "attempt=${attempt}, max_attempts=${SERVICES_START_RETRIES}; ${start_out}" "${start_rc}"
      if [[ "${attempt}" -lt "${SERVICES_START_RETRIES}" ]]; then
        sleep "${SERVICES_START_RETRY_DELAY_SEC}"
      fi
      attempt=$((attempt + 1))
    done

    recovery_diag=$(ssh_remote \
      "cd '${REMOTE_PATH}' && { docker compose ps -a; docker compose logs --no-color --tail=${REDIS_LOG_TAIL_LINES} '${REDIS_SERVICE_NAME}'; }" 2>&1)
    log_json "ERROR" "services_start_failed" "Не удалось запустить сервисы после повторных попыток" \
      "attempts=${SERVICES_START_RETRIES}; last_error=${start_out}; diagnostics=${recovery_diag}" "${start_rc}"
    return 1
  fi
  return 0
}

# shellcheck disable=SC2329
cleanup() {
  local original_rc="${1:-0}" final_rc
  final_rc="${original_rc}"
  if ! services_start_if_needed; then
    final_rc=1
  fi
  wg_down_if_needed
  [[ -n "${PROGRESS_METRICS_FILE}" ]] && rm -f "${PROGRESS_METRICS_FILE}" 2>/dev/null
  [[ -n "${DIAG_METRICS_FILE}" ]] && rm -f "${DIAG_METRICS_FILE}" 2>/dev/null
  cleanup_logs
  trap - EXIT INT TERM
  exit "${final_rc}"
}
trap 'cleanup "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

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
  if ping -c 1 -W 5 "${REMOTE_HOST}" >/dev/null 2>&1; then
    _wg_ok=1
    break
  fi
  log_json "WARN" "wg_ping_retry" "Попытка ${_attempt}/3: хост ${REMOTE_HOST} не отвечает"
  [[ "${_attempt}" -lt 3 ]] && sleep 5
done
if [[ "${_wg_ok}" -eq 0 ]]; then
  log_json "ERROR" "wg_no_connection" "Хост ${REMOTE_HOST} недоступен через VPN после 3 попыток" "" 1
  echo "Ошибка: ${REMOTE_HOST} недоступен через VPN" >&2
  exit 1
fi
log_json "INFO" "wg_connected" "Соединение с ${REMOTE_HOST} подтверждено"

###############################################################################
# PREFLIGHT
###############################################################################
for _wg_dep in wireguard-go ncat xxd; do
  if ! command -v "${_wg_dep}" >/dev/null 2>&1; then
    log_json "ERROR" "wg_dep_missing" "Не найдена зависимость для подъёма WireGuard" "opkg install ${_wg_dep}"
    echo "Ошибка: не найдена команда ${_wg_dep}. Установите: opkg install ${_wg_dep}" >&2
    exit 1
  fi
done

if [[ -n "${REMOTE_PASSWORD}" ]] && ! command -v sshpass >/dev/null 2>&1; then
  log_json "WARN" "sshpass_missing" "REMOTE_PASSWORD задан, но sshpass не найден — будет использован ssh по ключу" \
    "opkg install sshpass недоступен для этой архитектуры; настройте REMOTE_SSH_KEY"
fi

mkdir -p "${BACKUP_DIR}" 2>/dev/null
if [[ ! -d "${BACKUP_DIR}" ]]; then
  log_json "ERROR" "backup_dir_missing" "Папка назначения недоступна" "${BACKUP_DIR}"
  echo "Ошибка: папка ${BACKUP_DIR} недоступна" >&2
  exit 1
fi

###############################################################################
# BACKUP
###############################################################################
BACKUP_DATE="$(date '+%Y-%m-%d')"
REMOTE_PARENT="$(dirname "${REMOTE_PATH}")"
REMOTE_DIR="$(basename "${REMOTE_PATH}")"

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

# Порядок шифров SSH важен: он же и приоритет. chacha20-poly1305 стоит первым
# не просто так — на слабых ARM-чипах без аппаратных инструкций AES (типично
# для QNAP такого класса) программный AES-GCM/CTR заметно медленнее, чем
# chacha20-poly1305 (он спроектирован под быстрое ПРОГРАММНОЕ исполнение).
# Расшифровку входящего потока бэкапа выполняет SSH-клиент — он же и работает
# на QNAP (единственное ядро, см. backup_env_diag/local_load1 в логах), тогда
# как WireGuard-туннель добавляет СВОЙ отдельный слой шифрования (ChaCha20-
# Poly1305, зафиксирован протоколом WG, не настраивается) — то есть на QNAP
# каждый байт бэкапа расшифровывается ДВАЖДЫ на одном ядре. Смена приоритета
# шифра SSH не убирает этот двойной слой, но снижает долю CPU, которая уходит
# именно на SSH-часть, если до этого негociировался медленный на данном чипе
# AES. Можно переопределить через conf, если тесты покажут другой шифр быстрее.
SSH_CIPHERS="${SSH_CIPHERS:-chacha20-poly1305@openssh.com,aes128-gcm@openssh.com,aes256-gcm@openssh.com,aes128-ctr}"
SSH_OPTS=(
  -p "${REMOTE_SSH_PORT}"
  -o StrictHostKeyChecking=accept-new
  -o ConnectTimeout=30
  -o BatchMode=no
  -o Compression=no
  -c "${SSH_CIPHERS}"
)
[[ -n "${REMOTE_SSH_KEY}" ]] && SSH_OPTS+=(-i "${REMOTE_SSH_KEY}")

# Выполняет команду на удалённом хосте: sshpass+пароль, если sshpass найден и
# задан REMOTE_PASSWORD (как в десктопной версии), иначе обычный ssh по ключу
# (REMOTE_SSH_KEY / ssh-agent) — основной способ для QNAP, т.к. в Entware для
# этой архитектуры нет пакета sshpass.
ssh_remote() {
  if [[ -n "${REMOTE_PASSWORD}" ]] && command -v sshpass >/dev/null 2>&1; then
    SSHPASS="${REMOTE_PASSWORD}" sshpass -e ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" "$1"
  else
    # shellcheck disable=SC2029
    ssh "${SSH_OPTS[@]}" "${REMOTE_USER}@${REMOTE_HOST}" "$1"
  fi
}

###############################################################################
# ДИАГНОСТИКА НАГРУЗКИ (для расследования деградации скорости бэкапа)
#
# Наблюдение по логам за 2026-07-24..28: скорость архивирования почти
# постоянна В ПРЕДЕЛАХ одного запуска (не снижается плавно к концу), но
# отличается в ~2 раза МЕЖДУ разными запусками (~5 МиБ/с в одни дни,
# ~2.4-2.6 МиБ/с в другие) — это указывает на внешний фактор, действующий
# на весь запуск целиком (загрузка CPU/сети на одной из сторон), а не на
# деградацию из-за роста файла/буферов/температуры в процессе архивирования.
# Функции ниже дают факты (loadavg, свободная память, RTT) вместо догадки
# "probable_cause" по одной лишь средней скорости.
###############################################################################
get_local_load1() { awk '{print $1}' /proc/loadavg 2>/dev/null; }
get_local_mem_avail_mb() { awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo 2>/dev/null; }
get_local_nproc() { nproc 2>/dev/null || echo 1; }

# Блочное устройство под BACKUP_DIR (для чтения его счётчиков из
# /proc/diskstats). На Entware этой сборки нет iostat/vmstat (минимальный
# набор пакетов aarch64-k3.10 — только ash/bash/ncat/ssh/wg/zstd и т.п.),
# поэтому IO читаем напрямую из ядра тем же способом, что loadavg/meminfo
# выше — без установки дополнительных opkg-пакетов.
#
# Сопоставляем по major:minor, а не по имени: на QNAP BACKUP_DIR лежит на
# /dev/mapper/cachedevN, который сам по себе настоящий device-mapper узел
# (не symlink на dm-N — readlink -f возвращает его же самого), а в
# /proc/diskstats он зарегистрирован под другим именем (dm-N) с теми же
# major:minor (проверено вживую: cachedev1 → major:minor fb:9 hex → dm-9,
# 251:9 в diskstats). "-P" у df обязателен — без него BusyBox df переносит
# длинное имя устройства на отдельную строку, ломая разбор по NR==2.
#
# Пусто, если df/stat не смогли определить устройство или пары major:minor
# нет в diskstats — тогда IO просто не измеряется (n/a в логах), а не гадаем
# по несвязанному диску.
get_local_io_device() {
  local dev_path mm major minor
  dev_path=$(df -P "${BACKUP_DIR}" 2>/dev/null | awk 'NR==2{print $1}')
  [[ -n "${dev_path}" ]] || return
  mm=$(stat -c '%t %T' "${dev_path}" 2>/dev/null)
  [[ -n "${mm}" ]] || return
  major=$((16#$(printf '%s' "${mm}" | awk '{print $1}')))
  minor=$((16#$(printf '%s' "${mm}" | awk '{print $2}')))
  awk -v maj="${major}" -v min="${minor}" '$1==maj && $2==min{print $3; f=1} END{exit !f}' /proc/diskstats 2>/dev/null
}

# Печатает "sectors_read sectors_written io_ms" для устройства $1 из
# /proc/diskstats (поля 6, 10, 13 — см. Documentation/iostats.txt в ядре).
# io_ms ("time spent doing I/Os") — та же величина, на которой iostat считает
# %util: доля времени за интервал, когда на устройстве была хотя бы одна
# незавершённая операция.
get_local_io_raw() {
  awk -v d="$1" '$3==d{print $6, $10, $13}' /proc/diskstats 2>/dev/null
}

# Печатает 3 строки: loadavg(1мин), доступная память (МиБ), число ядер
# удалённого сервера — используется и как разовый baseline, и периодически.
get_remote_diag() {
  ssh_remote "awk '{print \$1}' /proc/loadavg 2>/dev/null; awk '/MemAvailable/{print int(\$2/1024)}' /proc/meminfo 2>/dev/null; nproc 2>/dev/null || echo 1" 2>/dev/null
}

# Среднее значение RTT (мс) до REMOTE_HOST по 3 пакетам; работает как с
# busybox ping ("round-trip min/avg/max = a/b/c ms"), так и с iputils
# ("rtt min/avg/max/mdev = a/b/c/d ms") — в обоих форматах avg второе число
# после "=".
get_rtt_ms() {
  ping -c 3 -W 2 "${REMOTE_HOST}" 2>/dev/null | tail -n1 | sed -nE 's#.*= *[0-9.]+/([0-9.]+)/[0-9.]+.*#\1#p'
}

# Снимок на удалённой стороне сразу после сбоя raw-transfer: loadavg,
# свободная память, свежие OOM-килы из dmesg (последние 3 мин) и место на
# диске REMOTE_PATH — чтобы не гонять это вручную по SSH после каждого
# падения, как пришлось делать при разборе инцидента 2026-07-31 (OOM убивал
# clamd прямо во время окна остановки/запуска сервисов вокруг бэкапа).
get_remote_failure_diag() {
  ssh_remote "echo loadavg=\$(awk '{print \$1}' /proc/loadavg 2>/dev/null); \
echo mem_avail_mb=\$(awk '/MemAvailable/{print int(\$2/1024)}' /proc/meminfo 2>/dev/null); \
echo disk_avail=\$(df -h '${REMOTE_PATH}' 2>/dev/null | awk 'NR==2{print \$4}'); \
oom=\$(dmesg -T 2>/dev/null | grep -i 'oom-kill\|out of memory' | tail -3 | tr '\n' ';'); \
echo oom_recent=\"\${oom:-none}\"" 2>/dev/null | tr '\n' ', '
}

# Замер реальной скорости сети QNAP<->REMOTE_HOST перед архивированием — тем
# же способом (nc -N на удалённой стороне + ncat --recv-only на QNAP), что и
# сама передача бэкапа, так что результат напрямую сравним с avg_mib_s из
# backup_metrics. Best-effort: любая ошибка (нет nc, порт занят, таймаут)
# просто логируется предупреждением и не прерывает бэкап — сеть уже проверена
# раньше через wg_check, это только диагностика скорости, а не связности.
measure_network_speed() {
  local _nc_check
  _nc_check="$(ssh_remote "command -v nc >/dev/null 2>&1 && echo yes" 2>/dev/null)"
  if [[ "${_nc_check}" != "yes" ]]; then
    log_json "WARN" "network_speed_test_skip_no_nc" "На удалённой стороне нет nc — замер скорости сети пропущен" ""
    return
  fi

  local test_status_file test_out test_launch test_start test_end test_dur test_bytes test_mib_s test_rc
  test_status_file="/tmp/.${SCRIPT_BASE}-speedtest-status-$$"
  test_out="$(mktemp)"

  test_launch="rm -f '${test_status_file}'; \
nohup bash -c 'dd if=/dev/zero bs=1M count=${NETWORK_SPEED_TEST_MB} 2>/dev/null | timeout 60 nc -N -l ${REMOTE_HOST} ${RAW_TRANSFER_PORT}; \
  echo \$? > \"${test_status_file}\"' </dev/null >/dev/null 2>&1 &"
  ssh_remote "${test_launch}" >/dev/null 2>&1
  sleep 1

  # На этом QNAP нет отдельного бинаря "timeout" (не входит ни в минимальный
  # набор Entware, ни в applet'ы системного busybox — проверено вживую,
  # "type timeout" не находит ничего). Раньше локальный ncat оборачивался в
  # "timeout 60 ncat ...", что всегда падало с rc=127 ("timeout: command not
  # found"), а не из-за самого ncat. Используем встроенный idle-таймаут ncat
  # вместо внешней обёртки — той же цели (не зависнуть навсегда), без
  # зависимости от отсутствующего бинаря.
  test_start=$(date +%s)
  ncat --recv-only --idle-timeout 60s "${REMOTE_HOST}" "${RAW_TRANSFER_PORT}" > "${test_out}" 2>/dev/null < /dev/null
  test_rc=$?
  test_end=$(date +%s)

  test_bytes=$(stat -c%s "${test_out}" 2>/dev/null || printf '0')
  rm -f "${test_out}"
  ssh_remote "cat '${test_status_file}' 2>/dev/null; rm -f '${test_status_file}'" >/dev/null 2>&1

  test_dur=$(( test_end - test_start ))
  [[ "${test_dur}" -lt 1 ]] && test_dur=1

  if [[ "${test_rc}" -eq 0 && "${test_bytes}" -gt 0 ]]; then
    test_mib_s=$(awk -v b="${test_bytes}" -v d="${test_dur}" 'BEGIN { printf "%.2f", (b/1048576)/d }')
    NETWORK_SPEED_TEST_MIB_S="${test_mib_s}"
    log_json "INFO" "network_speed_test" "Замер скорости сети перед началом бэкапа" \
      "bytes=${test_bytes}, duration_s=${test_dur}, mib_s=${test_mib_s}"
  else
    log_json "WARN" "network_speed_test_failed" "Не удалось измерить скорость сети (не влияет на сам бэкап)" \
      "bytes=${test_bytes}, duration_s=${test_dur}, ncat_rc=${test_rc}"
  fi
}

# Выбираем быстрейший доступный компрессор на удалённом сервере:
# zstd -3 --threads=0 > pigz -1 > gzip -1
REMOTE_COMP=$(ssh_remote \
  "if command -v zstd >/dev/null 2>&1; then echo zstd; elif command -v pigz >/dev/null 2>&1; then echo pigz; else echo gzip; fi" 2>/dev/null)
case "${REMOTE_COMP}" in
  zstd) COMP_CMD="zstd -3 --threads=0 -c";         BACKUP_EXT="tar.zst" ;;
  pigz) COMP_CMD="pigz -1";                       BACKUP_EXT="tar.gz"  ;;
  *)    COMP_CMD="gzip -1";                        BACKUP_EXT="tar.gz"  ;;
esac

# Снимок нагрузки ДО начала архивирования — baseline для сравнения с
# показателями во время передачи (см. backup_progress/backup_metrics ниже)
# и для расчёта probable_cause в backup_degradation.
LOCAL_NPROC="$(get_local_nproc)"
_local_load1_baseline="$(get_local_load1)"
_local_mem_avail_mb_baseline="$(get_local_mem_avail_mb)"
_remote_diag_baseline="$(get_remote_diag)"
_remote_load1_baseline=$(printf '%s\n' "${_remote_diag_baseline}" | sed -n '1p')
_remote_mem_avail_mb_baseline=$(printf '%s\n' "${_remote_diag_baseline}" | sed -n '2p')
REMOTE_NPROC=$(printf '%s\n' "${_remote_diag_baseline}" | sed -n '3p')
REMOTE_NPROC="${REMOTE_NPROC:-1}"
RTT_BASELINE_MS="$(get_rtt_ms)"
RTT_BASELINE_MS="${RTT_BASELINE_MS:-n/a}"
IO_DEVICE="$(get_local_io_device)"
log_json "INFO" "backup_env_diag" "Снимок нагрузки перед архивированием" \
  "local_nproc=${LOCAL_NPROC}, local_load1=${_local_load1_baseline:-n/a}, local_mem_avail_mb=${_local_mem_avail_mb_baseline:-n/a}, remote_nproc=${REMOTE_NPROC}, remote_load1=${_remote_load1_baseline:-n/a}, remote_mem_avail_mb=${_remote_mem_avail_mb_baseline:-n/a}, rtt_baseline_ms=${RTT_BASELINE_MS}, io_device=${IO_DEVICE:-n/a}"
if [[ -z "${IO_DEVICE}" ]]; then
  # На проде (2026-08-04) io_device вышел n/a — причина неизвестна (нет
  # прямого SSH-доступа к QNAP для живой проверки). Пишем сырой вывод df,
  # чтобы по следующему логу понять: df вообще не нашёл BACKUP_DIR, вернул
  # пустую строку, или вернул имя устройства, которого нет в /proc/diskstats
  # (например, длинное имя mapper-устройства на некоторых прошивках QTS).
  _io_debug_df="$(df "${BACKUP_DIR}" 2>&1 | tr '\n' ';')"
  log_json "WARN" "io_diag_unavailable" "Не удалось определить блочное устройство под BACKUP_DIR — замер IO (backup_resource_diag) пропущен" \
    "backup_dir=${BACKUP_DIR}, df_output=[${_io_debug_df:-empty}]"
fi

measure_network_speed

BACKUP_FILENAME="${SCRIPT_BASE}-${BACKUP_DATE}.${BACKUP_EXT}"
BACKUP_PATH="${BACKUP_DIR}/${BACKUP_FILENAME}"

if [[ -f "${BACKUP_PATH}" ]]; then
  if validate_backup_file "${BACKUP_PATH}"; then
    BACKUP_APPEND=1
    log_json "INFO" "backup_append_mode" "Файл бэкапа за ${BACKUP_DATE} уже существует и прошёл проверку — дозапись" \
      "file=${BACKUP_PATH}"
  else
    validate_rc=$?
    BACKUP_APPEND=0
    validate_detail="file=${BACKUP_PATH}"
    if [[ -n "${VALIDATE_BACKUP_DETAIL}" ]]; then
      validate_detail="${validate_detail}, reason=${VALIDATE_BACKUP_DETAIL}"
    fi
    if [[ ${validate_rc} -eq 1 ]]; then
      log_json "WARN" "backup_existing_invalid" "Существующий файл бэкапа повреждён — удаляем и создаём заново" \
        "${validate_detail}" "${validate_rc}"
      rm -f "${BACKUP_PATH}"
    else
      log_json "WARN" "backup_existing_unchecked" "Не удалось проверить существующий файл бэкапа — создаём заново без дозаписи" \
        "${validate_detail}" "${validate_rc}"
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
nextcloud_stack_state=$(ssh_remote \
  "cd '${REMOTE_PATH}' && docker compose ps -a '${NEXTCLOUD_SERVICE_NAME}'" 2>&1)
nextcloud_stack_state_rc=$?
if [[ "${nextcloud_stack_state_rc}" -eq 0 ]]; then
  log_json "INFO" "nextcloud_preflight" "Состояние сервиса Nextcloud перед очисткой" \
    "service=${NEXTCLOUD_SERVICE_NAME}; ${nextcloud_stack_state}" 0
else
  log_json "WARN" "nextcloud_preflight_failed" "Не удалось получить состояние сервиса Nextcloud" \
    "service=${NEXTCLOUD_SERVICE_NAME}; ${nextcloud_stack_state}" "${nextcloud_stack_state_rc}"
fi

# Приложения files_trashbin и files_versions в Nextcloud отключены
# (occ app:disable), поэтому у occ нет команд trashbin:cleanup/
# versions:cleanup (и нет самой папки data/<user>/files_trashbin, чинить
# которую был нужен отдельный repair-блок ниже, пока trashbin:cleanup её
# существования зависела) — оба шага пропускаются полностью, а не просто
# игнорируют ошибку.
log_json "INFO" "occ_trashbin_skipped" "Очистка корзин пропущена (files_trashbin отключено)" "" 0
log_json "INFO" "occ_versions_skipped" "Очистка версий файлов пропущена (files_versions отключено)" "" 0

###############################################################################
# DATABASE OPTIMIZATION (before services down)
###############################################################################
if [[ "${OPTIMIZE_MARIADB_BEFORE_BACKUP}" -eq 1 && "${DB_OPTIMIZE_DUE}" -eq 1 ]]; then
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
  if [[ ${mariadb_opt_rc} -ne 0 ]]; then
    log_json "WARN" "mariadb_optimize_failed" "Оптимизация MariaDB завершилась с предупреждениями" "${mariadb_opt_err}" "${mariadb_opt_rc}"
  else
    log_json "INFO" "mariadb_optimize_ok" "Оптимизация MariaDB завершена" "${mariadb_opt_err}" "${mariadb_opt_rc}"
  fi
elif [[ "${OPTIMIZE_MARIADB_BEFORE_BACKUP}" -eq 1 ]]; then
  log_json "INFO" "mariadb_optimize_skip_not_due" "Оптимизация MariaDB пропущена — не настал плановый интервал (раз в ${DB_OPTIMIZE_INTERVAL_DAYS} дн.)"
else
  log_json "INFO" "mariadb_optimize_skip" "Оптимизация MariaDB отключена (OPTIMIZE_MARIADB_BEFORE_BACKUP=0)"
fi

if [[ "${OPTIMIZE_REDIS_BEFORE_BACKUP}" -eq 1 && "${DB_OPTIMIZE_DUE}" -eq 1 ]]; then
  log_json "INFO" "redis_optimize_start" "Оптимизация Redis AOF перед архивированием"
  redis_opt_err=$(ssh_remote \
    "cd '${REMOTE_PATH}' && docker compose exec -T \
      -e REDIS_WAIT='${REDIS_REWRITE_WAIT_SEC}' \
      '${REDIS_SERVICE_NAME}' sh -lc 'set -e; \
        redis-cli BGREWRITEAOF >/dev/null; \
        i=0; \
        while [ \$i -lt \$REDIS_WAIT ]; do \
          in_progress=\$(redis-cli INFO persistence | tr -d '\\r' | sed -n \"s/^aof_rewrite_in_progress:\\([0-9]\\+\\)$/\\1/p\"); \
          [ \"\$in_progress\" = \"0\" ] && exit 0; \
          i=\$((i + 1)); \
          sleep 1; \
        done; \
        echo \"AOF rewrite did not finish within \$REDIS_WAIT seconds\"; \
        exit 1'" 2>&1)
  redis_opt_rc=$?
  if [[ ${redis_opt_rc} -ne 0 ]]; then
    log_json "WARN" "redis_optimize_failed" "Оптимизация Redis завершилась с предупреждениями" "${redis_opt_err}" "${redis_opt_rc}"
  else
    log_json "INFO" "redis_optimize_ok" "Оптимизация Redis завершена" "${redis_opt_err}" "${redis_opt_rc}"
  fi
elif [[ "${OPTIMIZE_REDIS_BEFORE_BACKUP}" -eq 1 ]]; then
  log_json "INFO" "redis_optimize_skip_not_due" "Оптимизация Redis пропущена — не настал плановый интервал (раз в ${DB_OPTIMIZE_INTERVAL_DAYS} дн.)"
else
  log_json "INFO" "redis_optimize_skip" "Оптимизация Redis отключена (OPTIMIZE_REDIS_BEFORE_BACKUP=0)"
fi

redis_logs_out=$(ssh_remote \
  "cd '${REMOTE_PATH}' && docker compose logs --no-color --tail=${REDIS_LOG_TAIL_LINES} '${REDIS_SERVICE_NAME}'" 2>&1)
redis_logs_rc=$?
log_json "INFO" "redis_logs_capture" "Логи Redis/Valkey перед остановкой сервисов (docker compose down удалит контейнер вместе с ними)" "${redis_logs_out}" "${redis_logs_rc}"

log_json "INFO" "services_stop" "Останавливаем сервисы на ${REMOTE_HOST}..."
stop_err=$(ssh_remote \
  "cd '${REMOTE_PATH}' && docker compose down" 2>&1)
stop_rc=$?
if [[ ${stop_rc} -ne 0 ]]; then
  log_json "WARN" "services_stop_failed" "Не удалось остановить сервисы" "${stop_err}" "${stop_rc}"
  echo "Предупреждение: не удалось остановить сервисы (код ${stop_rc}): ${stop_err}" >&2
else
  SERVICES_STOPPED=1
  log_json "INFO" "services_stop_ok" "Сервисы остановлены" "" "${stop_rc}"
  # Пауза перед стартом передачи: массовая остановка ~15 контейнеров (docker
  # compose down) на слабом железе ещё несколько секунд донастраивает сеть/DNS
  # на удалённой стороне после того, как сама команда уже вернула управление
  # (наблюдалось: raw-transfer падал с SIGPIPE через ~2с после запуска
  # листенера сразу после docker compose down). Даём хосту стабилизироваться.
  sleep 5
  log_json "INFO" "post_services_stop_settle" "Пауза после остановки сервисов для стабилизации сети хоста" "sleep_sec=5"
fi

if [[ "${DB_OPTIMIZE_DUE}" -eq 1 ]]; then
  db_optimize_mark_done
  log_json "INFO" "db_optimize_mark_done" "Плановая оптимизация БД отмечена выполненной — следующая через ${DB_OPTIMIZE_INTERVAL_DAYS} дн."
fi

PROGRESS_METRICS_FILE="$(mktemp)"
DIAG_METRICS_FILE="$(mktemp)"

# Фоновый монитор: каждые 60 сек пишет в лог размер файла и мгновенную
# скорость с прошлого замера (inst_mib_s — по ней видно, деградирует ли
# скорость ПЛАВНО в течение самого запуска, или она стабильна с начала до
# конца, а разница только между запусками). Раз в ~5 минут (каждый 5-й тик)
# дополнительно снимает нагрузку и свободную память на удалённом сервере и
# RTT до него — это отдельное SSH-соединение и 3 ping-пакета, поэтому не на
# каждом тике, чтобы не грузить слабое железо QNAP лишними TCP-хендшейками
# во время самой передачи. Раз в ~10 минут (каждый 10-й тик, подмножество
# 5-минутных) CPU/RAM/IO самого QNAP снимаются вместе одним замером
# (backup_resource_diag) — совместно, а не вразнобой, чтобы значения
# относились к одному и тому же моменту времени.
_progress_monitor() {
  local tick=0 prev_epoch="" prev_bytes=""
  local io_prev_epoch="" io_prev_read="" io_prev_write="" io_prev_ms=""
  while sleep 60; do
    [[ -f "${BACKUP_PATH}" ]] || break
    tick=$((tick + 1))
    local sz size_bytes now_epoch dt inst_mib_s diag_extra
    sz=$(du -sh "${BACKUP_PATH}" 2>/dev/null | cut -f1)
    size_bytes=$(stat -c%s "${BACKUP_PATH}" 2>/dev/null || wc -c < "${BACKUP_PATH}" 2>/dev/null || printf '0')
    now_epoch=$(date +%s)

    inst_mib_s="n/a"
    if [[ -n "${prev_epoch}" ]]; then
      dt=$((now_epoch - prev_epoch))
      [[ "${dt}" -gt 0 ]] && inst_mib_s=$(awk -v b1="${prev_bytes}" -v b2="${size_bytes}" -v d="${dt}" 'BEGIN { printf "%.2f", ((b2-b1)/1048576)/d }')
    fi
    prev_epoch="${now_epoch}"
    prev_bytes="${size_bytes}"

    diag_extra=""
    if [[ $((tick % 5)) -eq 0 ]]; then
      local remote_diag remote_load1 remote_mem_avail_mb rtt_ms local_load1 io_util_pct_field=""
      remote_diag="$(get_remote_diag)"
      remote_load1=$(printf '%s\n' "${remote_diag}" | sed -n '1p')
      remote_mem_avail_mb=$(printf '%s\n' "${remote_diag}" | sed -n '2p')
      rtt_ms="$(get_rtt_ms)"
      local_load1="$(get_local_load1)"
      diag_extra=", remote_load1=${remote_load1:-n/a}, remote_mem_avail_mb=${remote_mem_avail_mb:-n/a}, rtt_ms=${rtt_ms:-n/a}"

      if [[ $((tick % 10)) -eq 0 ]]; then
        local local_mem_avail_mb io_read_kib_s="n/a" io_write_kib_s="n/a" io_util_pct="n/a"
        local_mem_avail_mb="$(get_local_mem_avail_mb)"

        if [[ -n "${IO_DEVICE}" ]]; then
          local io_raw io_read io_write io_ms io_dt
          io_raw="$(get_local_io_raw "${IO_DEVICE}")"
          io_read=$(printf '%s' "${io_raw}" | awk '{print $1}')
          io_write=$(printf '%s' "${io_raw}" | awk '{print $2}')
          io_ms=$(printf '%s' "${io_raw}" | awk '{print $3}')
          if [[ -n "${io_prev_epoch}" && -n "${io_read}" ]]; then
            io_dt=$((now_epoch - io_prev_epoch))
            if [[ "${io_dt}" -gt 0 ]]; then
              io_read_kib_s=$(awk -v a="${io_prev_read}" -v b="${io_read}" -v d="${io_dt}" 'BEGIN { printf "%.1f", ((b-a)*512/1024)/d }')
              io_write_kib_s=$(awk -v a="${io_prev_write}" -v b="${io_write}" -v d="${io_dt}" 'BEGIN { printf "%.1f", ((b-a)*512/1024)/d }')
              io_util_pct=$(awk -v a="${io_prev_ms}" -v b="${io_ms}" -v d="${io_dt}" 'BEGIN { printf "%.1f", ((b-a)/(d*1000))*100 }')
              io_util_pct_field="${io_util_pct}"
            fi
          fi
          io_prev_epoch="${now_epoch}"; io_prev_read="${io_read}"; io_prev_write="${io_write}"; io_prev_ms="${io_ms}"
        fi

        log_json "INFO" "backup_resource_diag" "Совместный замер CPU/RAM/IO на QNAP" \
          "local_load1=${local_load1:-n/a}, local_mem_avail_mb=${local_mem_avail_mb:-n/a}, io_device=${IO_DEVICE:-n/a}, io_read_kib_s=${io_read_kib_s}, io_write_kib_s=${io_write_kib_s}, io_util_pct=${io_util_pct}"
      fi

      printf '%s\t%s\t%s\t%s\t%s\n' "${now_epoch}" "${remote_load1:-}" "${local_load1:-}" "${rtt_ms:-}" "${io_util_pct_field}" >> "${DIAG_METRICS_FILE}"
    fi

    printf '%s\t%s\n' "${now_epoch}" "${size_bytes}" >> "${PROGRESS_METRICS_FILE}"
    [[ -n "${sz}" ]] && log_json "INFO" "backup_progress" "Прогресс архивирования" \
      "size=${sz}, bytes=${size_bytes}, inst_mib_s=${inst_mib_s}${diag_extra}"
  done
}
_progress_monitor >/dev/null 2>&1 &
_PROGRESS_PID=$!

ARCHIVE_START_EPOCH=$(date +%s)
err_tmp=$(mktemp)
REMOTE_TAR_CMD="set -o pipefail; tar --create --file=- --sparse${REMOTE_TAR_EXCLUDE_ARGS} --directory='${REMOTE_PARENT}' '${REMOTE_DIR}' | ${COMP_CMD}"

# Решаем, доступна ли передача в обход SSH-шифрования (см. RAW_TRANSFER_ENABLED
# выше) — только если включена в conf И на удалённой стороне есть nc.
USE_RAW_TRANSFER=0
if [[ "${RAW_TRANSFER_ENABLED}" -eq 1 ]]; then
  _nc_check_raw="$(ssh_remote "command -v nc >/dev/null 2>&1 && echo yes" 2>/dev/null)"
  if [[ "${_nc_check_raw}" = "yes" ]]; then
    USE_RAW_TRANSFER=1
  else
    log_json "WARN" "raw_transfer_nc_missing" "На удалённом сервере не найден nc — используется передача через SSH" ""
  fi
fi

if [[ "${USE_RAW_TRANSFER}" -eq 1 ]]; then
  # Раздельные каналы: SSH запускает и останавливает удалённый листенер
  # (служебная команда), а сами байты бэкапа идут напрямую по TCP поверх
  # WireGuard, без дополнительного слоя SSH-шифрования поверх него.
  REMOTE_STATUS_FILE="/tmp/.${SCRIPT_BASE}-raw-status-$$"
  REMOTE_ERR_FILE="/tmp/.${SCRIPT_BASE}-raw-err-$$"
  # nohup + явный "bash -c" (а не логин-шелл, который может оказаться dash и
  # не понимать "set -o pipefail") запускает пайплайн в фоне на удалённой
  # стороне и переживает завершение этой SSH-сессии — ssh_remote() ниже
  # возвращается почти сразу, не дожидаясь всей передачи.
  RAW_LAUNCH_CMD="rm -f '${REMOTE_STATUS_FILE}' '${REMOTE_ERR_FILE}'; \
nohup bash -c 'set -o pipefail; tar --create --file=- --sparse${REMOTE_TAR_EXCLUDE_ARGS} \
  --directory=\"${REMOTE_PARENT}\" \"${REMOTE_DIR}\" | ${COMP_CMD} | \
  timeout ${RAW_TRANSFER_REMOTE_TIMEOUT_SEC} nc -N -l ${REMOTE_HOST} ${RAW_TRANSFER_PORT}; \
  echo \$? > \"${REMOTE_STATUS_FILE}\"' </dev/null >/dev/null 2>\"${REMOTE_ERR_FILE}\" &"
  RAW_LAUNCH_EPOCH=$(date +%s)
  ssh_remote "${RAW_LAUNCH_CMD}" >/dev/null 2>&1
  log_json "INFO" "raw_transfer_listener_launch" "Запущен приём бэкапа через raw TCP (порт ${RAW_TRANSFER_PORT}) в обход SSH-шифрования" ""

  raw_prev_size=0
  [[ "${BACKUP_APPEND}" -eq 1 ]] && raw_prev_size=$(stat -c%s "${BACKUP_PATH}" 2>/dev/null || printf '0')

  RAW_CONNECT_OK=0
  RAW_MID_STREAM_FAILURE=0
  for _raw_attempt in $(seq 1 "${RAW_TRANSFER_CONNECT_RETRIES}"); do
    # --recv-only обязателен: без него ncat ведёт себя как двунаправленный
    # прокси и, увидев мгновенный EOF на СВОЁМ stdin (из /dev/null), рвёт
    # приём почти сразу (воспроизведено вживую — обрыв на 16-48 КБ вместо
    # десятков гигабайт). --recv-only полностью отключает попытку читать/
    # слать stdin, приём при этом идёт до конца потока корректно.
    # На удалённой стороне "nc -N" (см. RAW_LAUNCH_CMD) обязателен по той же
    # причине с другой стороны: без -N OpenBSD nc не закрывает сокет по EOF
    # своего stdin и висит до RAW_TRANSFER_REMOTE_TIMEOUT_SEC (часы) — без
    # -N клиент с --recv-only корректно получает все данные, но не может
    # вовремя дождаться закрытия соединения. Комбинация nc-N + ncat
    # --recv-only проверена вживую на 50 и 100 МБ по несколько раз подряд —
    # каждый раз точный побайтовый результат и мгновенное завершение обеих
    # сторон.
    if [[ "${BACKUP_APPEND}" -eq 1 ]]; then
      ncat --recv-only "${REMOTE_HOST}" "${RAW_TRANSFER_PORT}" >> "${BACKUP_PATH}" 2>"${err_tmp}" < /dev/null
    else
      ncat --recv-only "${REMOTE_HOST}" "${RAW_TRANSFER_PORT}" > "${BACKUP_PATH}" 2>"${err_tmp}" < /dev/null
    fi
    _raw_rc=$?
    _raw_cur_size=$(stat -c%s "${BACKUP_PATH}" 2>/dev/null || printf '0')

    if [[ "${_raw_rc}" -eq 0 ]]; then
      RAW_CONNECT_OK=1
      break
    fi
    if [[ "${_raw_cur_size}" -gt "${raw_prev_size}" ]]; then
      # Байты уже пошли — листенер (он одноразовый, без -k) больше не
      # существует, повторное подключение невозможно в принципе. Это не
      # повод падать обратно на SSH-передачу (файл уже частично перезаписан
      # новыми данными) — это прямая ошибка бэкапа.
      RAW_MID_STREAM_FAILURE=1
      RAW_CONNECT_ELAPSED_S=$(( $(date +%s) - RAW_LAUNCH_EPOCH ))
      _mid_stderr=$(cat "${err_tmp}")
      _mid_diag=$(get_remote_failure_diag)
      log_json "WARN" "raw_transfer_mid_stream_failure" "Соединение разорвано в середине передачи — повтор невозможен" \
        "attempt=${_raw_attempt}, bytes=${_raw_cur_size}, elapsed_since_launch_s=${RAW_CONNECT_ELAPSED_S}, ncat_rc=${_raw_rc}, stderr=${_mid_stderr}, remote_diag=[${_mid_diag}]" "${_raw_rc}"
      break
    fi
    log_json "WARN" "raw_transfer_connect_retry" "Повтор подключения к порту передачи" \
      "attempt=${_raw_attempt}/${RAW_TRANSFER_CONNECT_RETRIES}, ncat_rc=${_raw_rc}" "${_raw_rc}"
    sleep 2
  done

  if [[ "${RAW_CONNECT_OK}" -eq 1 ]]; then
    # Чистый локальный EOF не доказывает, что tar|${COMP_CMD} на удалённой
    # стороне отработали успешно — nc просто ретранслирует байты до EOF,
    # ему неизвестен exit-код вышестоящих команд пайпа. Забираем реальный
    # статус из файла, который фоновый процесс пишет по завершении пайпа.
    RAW_CONNECT_ELAPSED_S=$(( $(date +%s) - RAW_LAUNCH_EPOCH ))
    raw_status=""
    for _raw_poll in $(seq 1 "${RAW_TRANSFER_STATUS_POLL_RETRIES}"); do
      raw_status=$(ssh_remote "cat '${REMOTE_STATUS_FILE}' 2>/dev/null")
      [[ -n "${raw_status}" ]] && break
      sleep 2
    done
    case "${raw_status}" in
      ''|*[!0-9]*)
        backup_rc=1
        err_out="raw transfer: remote status file missing/unreadable"
        _fail_diag=$(get_remote_failure_diag)
        log_json "WARN" "raw_transfer_status_missing" "Не удалось получить статус удалённого пайплайна" \
          "${err_out}, bytes=${_raw_cur_size}, elapsed_since_launch_s=${RAW_CONNECT_ELAPSED_S}, remote_diag=[${_fail_diag}]"
        ;;
      0)
        backup_rc=0
        err_out=""
        log_json "INFO" "raw_transfer_status_fetch" "Удалённый пайплайн (tar|${REMOTE_COMP:-gzip}) завершился успешно" \
          "bytes=${_raw_cur_size}, elapsed_since_launch_s=${RAW_CONNECT_ELAPSED_S}" 0
        ;;
      *)
        backup_rc="${raw_status}"
        _remote_err=$(ssh_remote "cat '${REMOTE_ERR_FILE}' 2>/dev/null")
        err_out="remote pipeline failed rc=${raw_status}: ${_remote_err}"
        _fail_diag=$(get_remote_failure_diag)
        log_json "WARN" "raw_transfer_status_fetch" "Удалённый пайплайн завершился с ошибкой" \
          "${err_out}, bytes=${_raw_cur_size}, elapsed_since_launch_s=${RAW_CONNECT_ELAPSED_S}, remote_diag=[${_fail_diag}]" "${backup_rc}"
        ;;
    esac
    ssh_remote "rm -f '${REMOTE_STATUS_FILE}' '${REMOTE_ERR_FILE}'" >/dev/null 2>&1
  elif [[ "${RAW_MID_STREAM_FAILURE}" -eq 1 ]]; then
    backup_rc=1
    err_out="raw transfer: connection dropped mid-stream after ${_raw_cur_size} bytes"
    ssh_remote "rm -f '${REMOTE_STATUS_FILE}' '${REMOTE_ERR_FILE}'" >/dev/null 2>&1
  else
    # Листенер так и не стал доступен ни разу за все попытки — в BACKUP_PATH
    # ничего нового не записано, безопасно вернуться к SSH-передаче.
    # Локальный stderr ncat различает только «Ncat: TIMEOUT.» (пакеты
    # дропает фаервол, листенер скорее всего жив и ждёт) и «Connection
    # refused» (порт открыт, но никто не слушает — удалённый пайплайн упал
    # на старте). Во втором случае причина лежит в REMOTE_ERR_FILE/
    # REMOTE_STATUS_FILE, поэтому вычитываем их ДО удаления — иначе лог
    # не позволяет отличить проблему с nc/tar на сервере от сетевой.
    err_out=$(cat "${err_tmp}")
    _remote_status=$(ssh_remote "cat '${REMOTE_STATUS_FILE}' 2>/dev/null")
    _remote_err=$(ssh_remote "cat '${REMOTE_ERR_FILE}' 2>/dev/null")
    _fail_diag=$(get_remote_failure_diag)
    log_json "WARN" "raw_transfer_connect_exhausted" "Не удалось подключиться к листенеру после ${RAW_TRANSFER_CONNECT_RETRIES} попыток — переход на передачу через SSH" \
      "${err_out}, remote_status=${_remote_status:-none}, remote_err=${_remote_err:-none}, remote_diag=[${_fail_diag}]"
    ssh_remote "rm -f '${REMOTE_STATUS_FILE}' '${REMOTE_ERR_FILE}'" >/dev/null 2>&1
    USE_RAW_TRANSFER=0
  fi
fi

if [[ "${USE_RAW_TRANSFER}" -eq 0 ]]; then
  [[ "${RAW_TRANSFER_ENABLED}" -eq 1 ]] && log_json "INFO" "raw_transfer_fallback_ssh" "Передача выполняется через SSH-туннель" ""
  if [[ "${BACKUP_APPEND}" -eq 1 ]]; then
    ssh_remote "${REMOTE_TAR_CMD}" \
      >> "${BACKUP_PATH}" 2>"${err_tmp}"
  else
    ssh_remote "${REMOTE_TAR_CMD}" \
      > "${BACKUP_PATH}" 2>"${err_tmp}"
  fi
  backup_rc=$?
  err_out=$(cat "${err_tmp}")
fi
rm -f "${err_tmp}"
ARCHIVE_END_EPOCH=$(date +%s)

kill "${_PROGRESS_PID}" 2>/dev/null
wait "${_PROGRESS_PID}" 2>/dev/null

if [[ "${backup_rc}" -eq 0 ]]; then
  archive_duration=$((ARCHIVE_END_EPOCH - ARCHIVE_START_EPOCH))
  [[ "${archive_duration}" -lt 1 ]] && archive_duration=1

  backup_bytes=$(stat -c%s "${BACKUP_PATH}" 2>/dev/null || wc -c < "${BACKUP_PATH}" 2>/dev/null || printf '0')
  backup_size=$(du -sh "${BACKUP_PATH}" 2>/dev/null | cut -f1)
  avg_mib_s=$(awk -v b="${backup_bytes}" -v d="${archive_duration}" 'BEGIN { printf "%.2f", (b/1048576)/d }')

  progress_samples=$(wc -l < "${PROGRESS_METRICS_FILE}" 2>/dev/null || printf '0')
  window_mib_s="n/a"
  if [[ "${progress_samples}" -ge 2 ]]; then
    first_sample=$(head -n1 "${PROGRESS_METRICS_FILE}")
    last_sample=$(tail -n1 "${PROGRESS_METRICS_FILE}")
    first_ts=$(printf '%s' "${first_sample}" | awk -F '\t' '{print $1}')
    first_bytes=$(printf '%s' "${first_sample}" | awk -F '\t' '{print $2}')
    last_ts=$(printf '%s' "${last_sample}" | awk -F '\t' '{print $1}')
    last_bytes=$(printf '%s' "${last_sample}" | awk -F '\t' '{print $2}')
    window_dt=$((last_ts - first_ts))
    if [[ "${window_dt}" -gt 0 ]]; then
      window_mib_s=$(awk -v b1="${first_bytes}" -v b2="${last_bytes}" -v d="${window_dt}" 'BEGIN { printf "%.2f", ((b2-b1)/1048576)/d }')
    fi
  fi

  # Средние значения нагрузки/RTT за весь запуск (из DIAG_METRICS_FILE,
  # который заполняется в _progress_monitor раз в ~5 минут) — фактические
  # данные для probable_cause ниже, вместо угадывания по одной лишь
  # средней скорости.
  diag_samples=$(wc -l < "${DIAG_METRICS_FILE}" 2>/dev/null || printf '0')
  avg_remote_load1="n/a"; avg_local_load1="n/a"; avg_rtt_ms="n/a"; avg_io_util_pct="n/a"
  if [[ "${diag_samples}" -ge 1 ]]; then
    avg_remote_load1=$(awk -F'\t' '$2!=""{s+=$2; c++} END{if (c>0) printf "%.2f", s/c; else print "n/a"}' "${DIAG_METRICS_FILE}")
    avg_local_load1=$(awk -F'\t' '$3!=""{s+=$3; c++} END{if (c>0) printf "%.2f", s/c; else print "n/a"}' "${DIAG_METRICS_FILE}")
    avg_rtt_ms=$(awk -F'\t' '$4!=""{s+=$4; c++} END{if (c>0) printf "%.2f", s/c; else print "n/a"}' "${DIAG_METRICS_FILE}")
    avg_io_util_pct=$(awk -F'\t' '$5!=""{s+=$5; c++} END{if (c>0) printf "%.1f", s/c; else print "n/a"}' "${DIAG_METRICS_FILE}")
  fi

  metrics_detail="file=${BACKUP_PATH}, size=${backup_size}, bytes=${backup_bytes}, duration_s=${archive_duration}, avg_mib_s=${avg_mib_s}, window_mib_s=${window_mib_s}, network_test_mib_s=${NETWORK_SPEED_TEST_MIB_S}, progress_samples=${progress_samples}, comp=${REMOTE_COMP:-gzip}, append=${BACKUP_APPEND}, local_nproc=${LOCAL_NPROC}, remote_nproc=${REMOTE_NPROC}, avg_local_load1=${avg_local_load1}, avg_remote_load1=${avg_remote_load1}, avg_rtt_ms=${avg_rtt_ms}, rtt_baseline_ms=${RTT_BASELINE_MS}, io_device=${IO_DEVICE:-n/a}, avg_io_util_pct=${avg_io_util_pct}"
  log_json "INFO" "backup_metrics" "Метрики этапа архивирования" "${metrics_detail}" 0

  if awk -v s="${avg_mib_s}" -v t="${BACKUP_DEGRADATION_MIBS_THRESHOLD}" 'BEGIN { exit !(s < t) }'; then
    probable_cause="unknown"
    if [[ "${progress_samples}" -lt 2 ]]; then
      probable_cause="insufficient_progress_samples"
    elif [[ "${NETWORK_SPEED_TEST_MIB_S}" != "n/a" ]] \
        && awk -v s="${NETWORK_SPEED_TEST_MIB_S}" -v t="${BACKUP_DEGRADATION_MIBS_THRESHOLD}" 'BEGIN { exit !(s < t) }'; then
      probable_cause="network_throughput_limit"
    elif [[ "${avg_remote_load1}" != "n/a" ]] && awk -v l="${avg_remote_load1}" -v n="${REMOTE_NPROC}" 'BEGIN { exit !(l >= n) }'; then
      probable_cause="remote_cpu_contention"
    elif [[ "${avg_local_load1}" != "n/a" ]] && awk -v l="${avg_local_load1}" -v n="${LOCAL_NPROC}" 'BEGIN { exit !(l >= n) }'; then
      probable_cause="local_cpu_or_io_contention"
    elif [[ "${avg_rtt_ms}" != "n/a" && "${RTT_BASELINE_MS}" != "n/a" ]] && awk -v r="${avg_rtt_ms}" -v b="${RTT_BASELINE_MS}" 'BEGIN { exit !(r > b * 1.5) }'; then
      probable_cause="network_latency_increase"
    elif [[ "${REMOTE_COMP:-gzip}" = "zstd" ]]; then
      probable_cause="network_or_remote_io_or_zstd_cpu"
    else
      probable_cause="network_or_remote_io"
    fi
    log_json "WARN" "backup_degradation" "Обнаружена деградация скорости бэкапа" \
      "threshold_mib_s=${BACKUP_DEGRADATION_MIBS_THRESHOLD}, probable_cause=${probable_cause}, ${metrics_detail}" 0
  fi

  backup_size=$(du -sh "${BACKUP_PATH}" 2>/dev/null | cut -f1)
  log_json "INFO" "backup_done" "Резервная копия создана успешно" \
    "file=${BACKUP_PATH}, size=${backup_size}" "${backup_rc}"
  echo "Готово: ${BACKUP_PATH} (${backup_size})"

  # Очистка старых бэкапов, оставляем только BACKUP_KEEP_COUNT последних
  # shellcheck disable=SC2012
  old_backups=$(ls -1t "${BACKUP_DIR}/${SCRIPT_BASE}-"*.tar.* 2>/dev/null | tail -n "+$((BACKUP_KEEP_COUNT + 1))")
  if [[ -n "${old_backups}" ]]; then
    log_json "INFO" "backup_cleanup" "Удаление старых резервных копий (оставляем ${BACKUP_KEEP_COUNT})"
    echo "${old_backups}" | tr '\n' '\0' | xargs -0 -r rm -f
  fi
else
  log_json "ERROR" "backup_failed" "Ошибка при создании резервной копии" "${err_out}" "${backup_rc}"
  echo "Ошибка резервного копирования (код ${backup_rc}): ${err_out}" >&2
  rm -f "${BACKUP_PATH}" 2>/dev/null
  exit "${backup_rc}"
fi

rm -f "${PROGRESS_METRICS_FILE}" "${DIAG_METRICS_FILE}" 2>/dev/null

exit 0
