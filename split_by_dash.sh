#!/bin/sh

set -eu

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_BASE=${SCRIPT_NAME%.*}
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)

LOG_DIR="${MYTASKS_LOG_DIR:-${SCRIPT_DIR}/logs}"
CONFIG_DIR="${SCRIPT_DIR}/conf"
CONFIG_FILE="${CONFIG_DIR}/${SCRIPT_BASE}.conf"
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"
TIMESTAMP="$(date '+%Y-%m-%d-%H-%M-%S')"
LOG_FILE="${LOG_DIR}/${SCRIPT_BASE}-${TIMESTAMP}.jsonl"
mkdir -p "${LOG_DIR}" "${CONFIG_DIR}"

CREATED_DIRS_FILE="$(mktemp)"
WIN_SAFE_FILE="$(mktemp)"
trap 'rm -f "${CREATED_DIRS_FILE}" "${WIN_SAFE_FILE}"' EXIT

# Функция win_safe() для awk: приводит одно имя файла/папки к допустимому в Windows
# (файлы синхронизируются Syncthing между Linux и Windows, недопустимые имена там
# не синхронизируются). Правила:
#   - символы < > : " \ | ? * и управляющие (0x00-0x1F) заменяются на «_»;
#   - ведущие пробелы, конечные точки и пробелы удаляются («Автор.» -> «Автор»);
#   - зарезервированные имена устройств (CON, PRN, AUX, NUL, COM1-9, LPT1-9) в
#     любом регистре и с любым расширением получают префикс «_» (CON.mp3 -> _CON.mp3).
# Длина компонента: лимит Linux (255 байт) строже лимита Windows (255 символов
# UTF-16), поэтому отдельной обрезки не требуется; пути длиннее MAX_PATH Syncthing
# в Windows обрабатывает через длинные пути.
cat > "${WIN_SAFE_FILE}" <<'AWK_EOF'
function win_safe(n,   i, b) {
  gsub(/[<>:"\\|?*[:cntrl:]]/, "_", n)
  sub(/^[[:space:]]+/, "", n)
  sub(/[.[:space:]]+$/, "", n)
  i = index(n, ".")
  b = (i > 0) ? substr(n, 1, i - 1) : n
  sub(/[[:space:]]+$/, "", b)
  if (toupper(b) ~ /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/) n = "_" n
  return n
}
AWK_EOF

if [ -r "${LOG_TEMPLATE_FILE}" ]; then
  # shellcheck source=/dev/null
  . "${LOG_TEMPLATE_FILE}"
fi
LOG_SCHEMA_VERSION="${LOG_SCHEMA_VERSION:-1.0}"
LOG_COMPAT_TARGETS="${LOG_COMPAT_TARGETS:-elk,opensearch,loki,graylog,splunk}"

###############################################################################
# ARGS
###############################################################################
DRY_RUN=0
DIR_ARG=""
for arg in "$@"; do
  case "${arg}" in
    -n|--dry-run) DRY_RUN=1 ;;
    -h|--help)
      echo "Использование: ${SCRIPT_NAME} [-n|--dry-run] [каталог]"
      echo "  -n, --dry-run  только показать, что будет создано/перемещено/удалено, без изменений"
      exit 0
      ;;
    *) [ -z "${DIR_ARG}" ] && DIR_ARG="${arg}" ;;
  esac
done

###############################################################################
# HELPERS
###############################################################################
ts() { date '+%Y-%m-%dT%H:%M:%S%z'; }

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\r//g'
}

log_json() {
  level="$1"
  event="$2"
  msg="$3"
  detail="${4:-}"
  level_norm="$(printf '%s' "${level}" | tr '[:upper:]' '[:lower:]')"
  msg_esc="$(json_escape "${msg}")"
  detail_esc="$(json_escape "${detail}")"
  _ts="$(ts)"
  printf '{"@timestamp":"%s","schema.version":"%s","compat.targets":"%s","log.level":"%s","message":"%s","event.action":"%s","service.name":"%s","script":"%s","event":"%s","level":"%s","msg":"%s","detail":"%s"}\n' \
    "${_ts}" "${LOG_SCHEMA_VERSION}" "${LOG_COMPAT_TARGETS}" "${level_norm}" "${msg_esc}" "${event}" "${SCRIPT_BASE}" "${SCRIPT_NAME}" "${event}" "${level_norm}" "${msg_esc}" "${detail_esc}" >> "${LOG_FILE}"
}

cleanup_logs() {
  find "${LOG_DIR}" -maxdepth 1 -type f -name "${SCRIPT_BASE}-*.jsonl" -printf '%T@|%p\n' 2>/dev/null \
    | sort -nr \
    | awk -F'|' 'NR > 10 { print $2 }' \
    | while IFS= read -r old_log; do
        [ -n "${old_log}" ] && rm -f -- "${old_log}"
      done
}

trim() {
  printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

# Переносит содержимое каталога src в каталог dst (при конфликте имён добавляет
# числовой суффикс, как и при раскладке файлов); пустой src затем удаляется.
merge_dir_into() {
  src="$1"
  dst="$2"
  # В dry-run папка-источник может ещё не существовать (будет создана ранее в плане)
  [ -d "${src}" ] || return 0
  find "${src}" -mindepth 1 -maxdepth 1 | while IFS= read -r item; do
    item_name=$(basename -- "${item}")
    item_safe=$(win_safe_name "${item_name}")
    [ -n "${item_safe}" ] || item_safe="${item_name}"
    item_free=$(free_name "${dst}" "${item_safe}")
    item_dst="${dst}/${item_free}"
    if [ "${DRY_RUN}" -eq 1 ] || mv -f -- "${item}" "${item_dst}"; then
      log_action "INFO" "moved_item" "Файл перемещен при объединении папок" "${item} -> ${item_dst}"
    fi
  done
  do_rmdir "${src}"
}

# Объединяет папки, отличающиеся от canon только регистром (в том числе «Имя Фамилия»
# с пробелами и кириллицей), в одну — с точным регистром canon. Если папки canon нет,
# она создаётся; содержимое остальных переносится в неё, остальные удаляются.
# Регистр сравнивается через awk tolower() в UTF-8 (нужен gawk; mawk кириллицу не понимает).
merge_case_variants() {
  parent="$1"
  canon="$2"
  variants=$(find "${parent}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
    | CANON="${canon}" LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e 'BEGIN { c = ENVIRON["CANON"]; w = tolower(c) } { n = win_safe($0) } tolower(n) == w && $0 != c')
  [ -n "${variants}" ] || return 0

  canon_dir="${parent}/${canon}"
  if [ ! -d "${canon_dir}" ]; then
    do_mkdir "${canon_dir}"
    log_action "INFO" "mkdir" "Создана папка исполнителя" "${canon_dir}"
    echo "${canon_dir}" >> "${CREATED_DIRS_FILE}"
  fi
  printf '%s\n' "${variants}" | while IFS= read -r variant; do
    [ -n "${variant}" ] || continue
    variant_dir="${parent}/${variant}"
    merge_dir_into "${variant_dir}" "${canon_dir}"
    if [ "${DRY_RUN}" -eq 0 ] && [ -d "${variant_dir}" ]; then
      log_json "ERROR" "case_merge_failed" "Не удалось удалить папку-дубликат другого регистра" "${variant_dir}"
    else
      log_action "INFO" "case_merged" "Папка другого регистра объединена с папкой исполнителя" "${variant_dir} -> ${canon_dir}"
    fi
  done
}

# Объединяет внутри каталога parent все папки (только прямые потомки), различающиеся
# лишь регистром, не затрагивая другие каталоги (в том числе вложенные и целевые).
# Эталон выбирает merge_case_group; остальные папки переносятся в него и удаляются.
merge_case_siblings() {
  parent="$1"
  [ -d "${parent}" ] || return 0
  group_file="$(mktemp)"
  find "${parent}" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null \
    | LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e '
        { s = win_safe($0); if (s == "") next
          k = tolower(s); n[k]++; v[k] = (k in v) ? v[k] "\n" $0 : $0; if (s != $0) dot[k] = 1 }
        END {
          for (k in n) if (n[k] > 1 || (k in dot)) {
            m = split(v[k], a, "\n"); asort(a)
            for (i = 1; i <= m; i++) print a[i]
            print ""
          }
        }' \
    | while IFS= read -r name; do
        if [ -n "${name}" ]; then
          printf '%s\n' "${name}" >> "${group_file}"
        else
          merge_case_group "${parent}" "${group_file}"
          : > "${group_file}"
        fi
      done
  rm -f -- "${group_file}"
}

# Оценка «внешнего вида» регистра имени: три числа через пробел —
# 1) 1, если имя целиком в верхнем регистре (HURTS), иначе 0;
# 2) 1, если первая буква не заглавная, иначе 0;
# 3) число заглавных букв, кроме первого символа.
# Чем меньше значения, тем ближе имя к виду «Первая заглавная, остальные строчные».
case_rank() {
  printf '%s\n' "$1" | LC_ALL=C.UTF-8 awk '{
    n = length($0); up = 0; first_low = 1
    for (i = 1; i <= n; i++) {
      c = substr($0, i, 1)
      if (c != tolower(c) && c == toupper(c)) { if (i == 1) first_low = 0; else up++ }
    }
    all_caps = ($0 != tolower($0) && $0 == toupper($0)) ? 1 : 0
    print all_caps, first_low, up
  }'
}

# Объединяет одну группу папок (имена в файле group_file) внутри parent в одну.
# Имена сравниваются без конечных точек; итоговое имя — без них. Эталон:
# 1) не «КАПСОМ» целиком (HURTS проигрывает Hurts даже при большем числе файлов);
# 2) больше файлов; 3) ближе к «Первая заглавная, остальные строчные» (первая буква
# заглавная, меньше прочих заглавных); 4) первая по кодовым точкам.
# Группа из одной папки с конечной точкой просто переименовывается.
merge_case_group() {
  parent="$1"
  group_file="$2"
  best=""
  best_n=-1
  best_caps=0
  best_low=0
  best_up=0
  while IFS= read -r member; do
    member_n=$(find "${parent}/${member}" -type f | wc -l)
    member_clean=$(win_safe_name "${member}")
    member_rank=$(case_rank "${member_clean}")
    read -r m_caps m_low m_up <<RANK_EOF
${member_rank}
RANK_EOF
    better=0
    if [ "${best_n}" -lt 0 ]; then
      better=1
    elif [ "${m_caps}" -ne "${best_caps}" ]; then
      [ "${m_caps}" -lt "${best_caps}" ] && better=1
    elif [ "${member_n}" -ne "${best_n}" ]; then
      [ "${member_n}" -gt "${best_n}" ] && better=1
    elif [ "${m_low}" -ne "${best_low}" ]; then
      [ "${m_low}" -lt "${best_low}" ] && better=1
    elif [ "${m_up}" -lt "${best_up}" ]; then
      better=1
    fi
    if [ "${better}" -eq 1 ]; then
      best="${member_clean}"
      best_n="${member_n}"
      best_caps="${m_caps}"
      best_low="${m_low}"
      best_up="${m_up}"
    fi
  done < "${group_file}"

  best_dir="${parent}/${best}"
  if [ ! -d "${best_dir}" ]; then
    do_mkdir "${best_dir}"
    log_action "INFO" "mkdir" "Создана папка без конечных точек" "${best_dir}"
  fi

  while IFS= read -r member; do
    [ "${member}" = "${best}" ] && continue
    merge_dir_into "${parent}/${member}" "${best_dir}"
    if [ "${DRY_RUN}" -eq 0 ] && [ -d "${parent}/${member}" ]; then
      log_json "ERROR" "case_merge_failed" "Не удалось удалить папку-дубликат" "${parent}/${member}"
    else
      log_action "INFO" "case_merged" "Папка объединена с эталонной (регистр/конечные точки)" "${parent}/${member} -> ${best_dir}"
    fi
  done < "${group_file}"
}

# Приводит имя к допустимому в Windows (см. win_safe в начале файла).
win_safe_name() {
  printf '%s\n' "$1" | LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e '{ print win_safe($0) }'
}

# Печатает имя первой записи каталога dir, совпадающей с name без учёта регистра
# (исключая запись exclude). Windows не различает регистр, поэтому такие имена —
# конфликт.
ci_find() {
  find "$1" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null \
    | NAME="$2" EXCL="${3:-}" LC_ALL=C.UTF-8 awk 'BEGIN { w = tolower(ENVIRON["NAME"]) } tolower($0) == w && $0 != ENVIRON["EXCL"] { print; exit }'
}

# Печатает свободное в каталоге dir имя: name, а при точном совпадении — name.1,
# name.2 … Имена, отличающиеся только регистром, конфликтом здесь не считаются: такие
# файлы потом разбирает sanitize_names (остаётся больший по размеру).
free_name() {
  _fn_cand="$2"
  _fn_i=1
  while [ -e "$1/${_fn_cand}" ]; do
    _fn_cand="$2.${_fn_i}"
    _fn_i=$((_fn_i+1))
  done
  printf '%s\n' "${_fn_cand}"
}

# Лог действия, меняющего файловую систему. В режиме dry-run действие только
# печатается и пишется в лог с событием dry_<event>.
log_action() {
  if [ "${DRY_RUN}" -eq 1 ]; then
    printf '[dry-run] будет: %s: %s\n' "$3" "$4"
    log_json "$1" "dry_$2" "[dry-run] $3" "$4"
  else
    log_json "$1" "$2" "$3" "$4"
  fi
}

# Обёртки над операциями, меняющими файловую систему: в dry-run ничего не делают.
# Перемещение (mv) в dry-run пропускается прямо в условии: `[ "${DRY_RUN}" -eq 1 ] || mv ...`.
do_mkdir() {
  [ "${DRY_RUN}" -eq 1 ] && return 0
  mkdir -p -- "$1"
}

do_rmdir() {
  [ "${DRY_RUN}" -eq 1 ] && return 0
  rmdir -- "$1" 2>/dev/null || true
}

# Переименовывает запись path в допустимое для Windows имя. Папка, у которой имя
# после исправления совпадает (без учёта регистра) с существующей папкой,
# объединяется с ней; файл при конфликте получает суффикс .N.
rename_safe() {
  path="$1"
  dir=$(dirname -- "${path}")
  base=$(basename -- "${path}")
  new=$(win_safe_name "${base}")
  if [ -z "${new}" ]; then
    log_json "WARN" "unsafe_name_unfixable" "Имя нельзя привести к допустимому для Windows" "${path}"
    return 0
  fi
  existing=$(ci_find "${dir}" "${new}" "${base}")
  if [ -d "${path}" ] && [ -n "${existing}" ] && [ -d "${dir}/${existing}" ]; then
    merge_dir_into "${path}" "${dir}/${existing}"
    log_action "INFO" "renamed_safe" "Папка с недопустимым для Windows именем объединена с существующей" "${path} -> ${dir}/${existing}"
    return 0
  fi
  new_free=$(free_name "${dir}" "${new}")
  target="${dir}/${new_free}"
  if [ "${DRY_RUN}" -eq 1 ] || mv -f -- "${path}" "${target}"; then
    log_action "INFO" "renamed_safe" "Имя приведено к допустимому для Windows" "${path} -> ${target}"
  fi
}

# Из группы файлов (пути в group_file, по одному в строке), различающихся только
# регистром имени, оставляет самый большой по размеру (при равенстве — первый в
# порядке сортировки по кодовым точкам); остальные удаляются.
resolve_case_files() {
  group_file="$1"
  keep=""
  keep_size=-1
  while IFS= read -r candidate; do
    candidate_size=$(stat -c %s -- "${candidate}")
    if [ "${candidate_size}" -gt "${keep_size}" ]; then
      keep="${candidate}"
      keep_size="${candidate_size}"
    fi
  done < "${group_file}"
  while IFS= read -r candidate; do
    [ "${candidate}" = "${keep}" ] && continue
    candidate_size=$(stat -c %s -- "${candidate}")
    if [ "${DRY_RUN}" -eq 1 ] || rm -f -- "${candidate}"; then
      log_action "INFO" "case_dup_removed" "Удалён файл меньшего размера, отличавшийся от оставленного только регистром имени (${candidate_size} Б, оставлен ${keep_size} Б)" "${candidate} (оставлен ${keep})"
    fi
  done < "${group_file}"
}

# Приводит к допустимым в Windows имена всех записей внутри каталога parent на любой
# глубине (глубокие — первыми, чтобы пути родителей оставались верными), затем
# разбирает пары файлов, различающихся только регистром (Windows хранит только один):
# остаётся больший по размеру. Одноимённые по регистру папки (глубже первого уровня)
# лишь выводятся в предупреждении: содержимое объединять автоматически нельзя.
sanitize_names() {
  parent="$1"
  [ -d "${parent}" ] || return 0
  fix_list="$(mktemp)"
  # Папки первого уровня исправляет merge_case_siblings (с объединением), поэтому
  # здесь они пропускаются: формат строки «глубина|тип|путь».
  find "${parent}" -mindepth 1 -depth -printf '%d|%y|%p\n' 2>/dev/null \
    | LC_ALL=C.UTF-8 awk -F'|' -f "${WIN_SAFE_FILE}" -e '
        { path = $0; sub(/^[^|]*\|[^|]*\|/, "", path)
          if ($1 == 1 && $2 == "d") next
          n = path; sub(/.*\//, "", n)
          if (win_safe(n) != n) print path }' > "${fix_list}"
  while IFS= read -r unsafe_path; do
    rename_safe "${unsafe_path}"
  done < "${fix_list}"
  : > "${fix_list}"
  # Группы с совпадающим именем без учёта регистра: «тип|путь», группы разделены
  # пустой строкой; внутри группы пути отсортированы
  find "${parent}" -mindepth 1 -printf '%y|%p\n' 2>/dev/null \
    | LC_ALL=C.UTF-8 awk -F'|' '
        { t = $1; path = $0; sub(/^[^|]*\|/, "", path)
          n = path; d = path; sub(/.*\//, "", n); sub(/\/[^\/]*$/, "", d)
          k = d "\034" tolower(n); c[k]++; v[k] = (k in v) ? v[k] "\n" t "|" path : t "|" path }
        END { for (k in c) if (c[k] > 1) { m = split(v[k], a, "\n"); asort(a); for (i = 1; i <= m; i++) print a[i]; print "" } }' > "${fix_list}"
  group_file="$(mktemp)"
  : > "${group_file}"
  while IFS= read -r entry; do
    if [ -n "${entry}" ]; then
      case "${entry}" in
        f\|*) printf '%s\n' "${entry#f|}" >> "${group_file}" ;;
        *)
          echo "Внимание: папки различаются только регистром (Windows их не различает): ${entry#*|}"
          log_json "WARN" "case_collision" "Папки различаются только регистром" "${entry#*|}"
          ;;
      esac
    else
      if [ -s "${group_file}" ]; then
        resolve_case_files "${group_file}"
      fi
      : > "${group_file}"
    fi
  done < "${fix_list}"
  rm -f -- "${fix_list}" "${group_file}"
}

process_directory() {
  current_dir="$1"
  if [ ! -d "${current_dir}" ]; then
    log_json "ERROR" "dir_missing" "Каталог не существует" "${current_dir}"
    return
  fi

  log_json "INFO" "dir_start" "Обработка директории" "${current_dir}"
  find "${current_dir}" -maxdepth 1 -type f | while IFS= read -r file; do
    name=$(basename -- "${file}")
    folder_raw=$(printf '%s\n' "${name}" | sed -n 's/^\(.*\)[[:space:]][-–—][[:space:]].*/\1/p' | sed 's/[[:space:]]*$//' | sed -n '1p')
    [ -z "${folder_raw}" ] && continue
    # Имена приводятся к допустимым в Windows: в имени папки («Автор.»), в части
    # «Автор» внутри имени файла и во всём имени файла («песня.mp3.»)
    folder=$(win_safe_name "${folder_raw}")
    [ -z "${folder}" ] && continue
    rest="${name#"${folder_raw}"}"
    new_name=$(win_safe_name "${folder}${rest}")
    [ -z "${new_name}" ] && continue

    target_dir="${current_dir}/${folder}"
    # Папки, различающиеся только регистром, сливаем в папку с регистром исполнителя;
    # проверка один раз на папку, а не на каждый файл.
    if ! grep -qxF -- "${target_dir}" "${CREATED_DIRS_FILE}"; then
      merge_case_variants "${current_dir}" "${folder}"
    fi
    if [ ! -d "${target_dir}" ] && ! grep -qxF -- "${target_dir}" "${CREATED_DIRS_FILE}"; then
      log_action "INFO" "mkdir" "Создана папка исполнителя" "${target_dir}"
    fi
    do_mkdir "${target_dir}"
    echo "${target_dir}" >> "${CREATED_DIRS_FILE}"

    free=$(free_name "${target_dir}" "${new_name}")
    dst="${target_dir}/${free}"

    if [ "${DRY_RUN}" -eq 1 ] || mv -f -- "${file}" "${dst}"; then
      log_action "INFO" "moved" "Файл перемещен" "${name} -> ${dst}"
    fi
  done
  # Папки внутри обработанного каталога сравниваются только между собой
  merge_case_siblings "${current_dir}"
  sanitize_names "${current_dir}"
}

move_created_dirs() {
  dest_dir="$1"
  if [ "${DRY_RUN}" -eq 0 ]; then
    mkdir -p -- "${dest_dir}" 2>/dev/null || true
  fi
  if [ "${DRY_RUN}" -eq 0 ] && [ ! -d "${dest_dir}" ]; then
    log_json "ERROR" "dest_missing" "Целевой каталог для переноса недоступен" "${dest_dir}"
    return
  fi

  log_json "INFO" "move_start" "Перенос получившихся папок в целевой каталог" "${dest_dir}"
  sort -u "${CREATED_DIRS_FILE}" | while IFS= read -r dir; do
    [ -d "${dir}" ] || [ "${DRY_RUN}" -eq 1 ] || continue
    dir_name=$(basename -- "${dir}")
    dir_dst="${dest_dir}/${dir_name}"

    if [ "${dir}" = "${dir_dst}" ]; then
      continue
    fi

    if [ -e "${dir_dst}" ]; then
      merge_dir_into "${dir}" "${dir_dst}"
      log_action "INFO" "merged_dir" "Папка объединена с существующей в целевом каталоге" "${dir} -> ${dir_dst}"
    else
      if [ "${DRY_RUN}" -eq 1 ] || mv -f -- "${dir}" "${dir_dst}"; then
        log_action "INFO" "moved_dir" "Папка перемещена в целевой каталог" "${dir} -> ${dir_dst}"
      else
        log_json "ERROR" "move_dir_failed" "Не удалось переместить папку в целевой каталог" "${dir} -> ${dir_dst}"
      fi
    fi
  done
}

###############################################################################
# MAIN
###############################################################################
DEST_DIR=""
if [ -r "${CONFIG_FILE}" ]; then
  DEST_DIR=$(sed -n 's/^[[:space:]]*DEST_DIR[[:space:]]*=[[:space:]]*//p' "${CONFIG_FILE}" | sed 's/#.*//' | tail -n1)
  DEST_DIR=$(trim "${DEST_DIR}")
fi

if [ -n "${DIR_ARG}" ] && [ -d "${DIR_ARG}" ]; then
  process_directory "${DIR_ARG}"
elif [ -r "${CONFIG_FILE}" ]; then
  log_json "INFO" "config_used" "Использование конфига" "${CONFIG_FILE}"
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in ""|\#*) continue ;; *) ;; esac
    case "${line}" in *DEST_DIR=*) continue ;; *) ;; esac
    target=$(trim "$({ printf '%s' "${line}" | sed 's/#.*//'; } || true)")
    [ -n "${target}" ] && process_directory "${target}"
  done < "${CONFIG_FILE}"
else
  err="Ошибка: каталог не указан и конфиг ${CONFIG_FILE} не найден."
  echo "${err}" >&2
  log_json "ERROR" "config_missing" "${err}"
  cleanup_logs
  exit 1
fi

if [ -n "${DEST_DIR}" ]; then
  move_created_dirs "${DEST_DIR}"
  # Целевой каталог проверяется отдельно, независимо от обработанных каталогов
  merge_case_siblings "${DEST_DIR}"
  sanitize_names "${DEST_DIR}"
fi

if [ "${DRY_RUN}" -eq 1 ]; then
  echo "[dry-run] Изменения не выполнены."
fi

cleanup_logs
exit 0