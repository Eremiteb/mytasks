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
# Список имён-исключений: эти имена файлов/папок win_safe() оставляет как есть
# (по одному на строку, точное совпадение, «#» — комментарий)
SPLIT_KEEP_FILE="${CONFIG_DIR}/${SCRIPT_BASE}.keep.conf"
export SPLIT_KEEP_FILE
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
#   - «&» всегда с одним пробелом с обеих сторон («A&B», «A &B» -> «A & B»), кроме
#     ведущего/конечного «&» («&ME»); «&amp;» -> «&»;
#   - зарезервированные имена устройств (CON, PRN, AUX, NUL, COM1-9, LPT1-9) в
#     любом регистре и с любым расширением получают префикс «_» (CON.mp3 -> _CON.mp3).
# Имена из conf/split_by_dash.keep.conf (SPLIT_KEEP_FILE) не меняются вообще.
# Длина компонента: лимит Linux (255 байт) строже лимита Windows (255 символов
# UTF-16), поэтому отдельной обрезки не требуется; пути длиннее MAX_PATH Syncthing
# в Windows обрабатывает через длинные пути.
cat > "${WIN_SAFE_FILE}" <<'AWK_EOF'
BEGIN {
  keep_file = ENVIRON["SPLIT_KEEP_FILE"]
  while (keep_file != "" && (getline keep_line < keep_file) > 0) {
    if (keep_line !~ /^[[:space:]]*(#|$)/) keep[keep_line] = 1
  }
  # Буквы с диакритикой, «ё» и казахские буквы для ключа сравнения («Beyoncé»/«Beyonce»,
  # «Ёлка»/«Елка», «Қайрат»/«Кайрат»; «й» и «и» не отождествляются)
  pairs = "à:a á:a â:a ã:a ä:a å:a ā:a ă:a ą:a ç:c ć:c č:c ď:d è:e é:e ê:e ë:e ē:e ě:e ę:e ì:i í:i î:i ï:i ī:i ł:l ñ:n ń:n ň:n ò:o ó:o ô:o õ:o ö:o ø:o ō:o ő:o ř:r ś:s š:s ş:s ť:t ù:u ú:u û:u ü:u ū:u ů:u ű:u ý:y ÿ:y ž:z ź:z ż:z ё:е ә:а ғ:г қ:к ң:н ө:о ұ:у ү:у һ:х і:и"
  np = split(pairs, pp, " ")
  for (pi = 1; pi <= np; pi++) { split(pp[pi], kv, ":"); fold[kv[1]] = kv[2] }
}
# Заменяет буквы с диакритикой и «ё» на базовые (вход — нижний регистр)
function foldchars(s,   i, n, c, out) {
  out = ""
  n = length(s)
  for (i = 1; i <= n; i++) { c = substr(s, i, 1); out = out ((c in fold) ? fold[c] : c) }
  return out
}
# Убирает ведущий артикль «The » в имени папки исполнителя («The Rasmus» -> «Rasmus»);
# если после него ничего не остаётся, имя не меняется
function strip_the(n,   r) {
  if (tolower(substr(n, 1, 4)) == "the " ) {
    r = substr(n, 5)
    sub(/^[[:space:]]+/, "", r)
    if (r != "") return r
  }
  return n
}
# 1, если в имени есть «ё/Ё» (вариант с «ё» предпочтителен)
function has_yo(s) {
  return (index(tolower(s), "ё") > 0) ? 1 : 0
}
# 1, если в имени есть латинская буква с диакритикой (Beyoncé, Måneskin): такой вариант
# НЕ эталон, эталоном остаётся написание без диакритики
function has_dia(s,   i, n, c) {
  s = tolower(s)
  n = length(s)
  for (i = 1; i <= n; i++) { c = substr(s, i, 1); if ((c in fold) && index("ёәғқңөұүһі", c) == 0) return 1 }
  return 0
}
# 1, если в имени есть казахская буква (ә ғ қ ң ө ұ ү һ і): такой вариант предпочтителен
function has_kz(s,   i, n, c) {
  s = tolower(s)
  n = length(s)
  for (i = 1; i <= n; i++) { c = substr(s, i, 1); if (index("әғқңөұүһі", c) > 0) return 1 }
  return 0
}
# Ключ имени для сравнения папок: нижний регистр, самостоятельные слова «и»/«and»
# отброшены (скачивание иногда склеивает соавторов без них: «Виктор Рыбин и Наталья
# Сенчукова» = «Виктор РыбинНаталья Сенчукова»), только буквы и цифры.
function nkey(s,   t, u) {
  s = strip_the(foldchars(tolower(s)))
  t = drop_words(s)
  u = t
  gsub(/[^[:alnum:]]/, "", u)
  if (u == "") t = s   # имя целиком из служебных слов — не обнуляем
  gsub(/[^[:alnum:]]/, "", t)
  return t
}
# Убирает самостоятельные служебные слова («и», «and», «группа», «тобы», «тобі», «ансамбль»,
# «band», «official»): «Жігіттер тобы» -> «Жігіттер»; вход — нижний регистр
function drop_words(s) {
  s = " " s " "
  gsub(/[[:space:]]+(и|and|группа|тобы|тобі|ансамбль|band|official)[[:space:]]+/, " ", s)
  gsub(/[[:space:]]+(и|and|группа|тобы|тобі|ансамбль|band|official)[[:space:]]+/, " ", s)
  return s
}
# 1, если в имени есть служебное слово (группа, тобы, тобі, ансамбль, band, official):
# такой вариант предпочтителен (Жігіттер тобы, Группа губы)
function has_noise(s) {
  return ((" " tolower(s) " ") ~ /[[:space:]](группа|тобы|тобі|ансамбль|band|official)[[:space:]]/) ? 1 : 0
}
function win_safe(n,   i, b, lead, tail) {
  if (n in keep) return n
  # HTML-экранирование «&amp;» из названий сайтов -> «&»
  gsub(/&amp;/, "\\&", n)
  gsub(/[<>:"\\|?*[:cntrl:]]/, "_", n)
  sub(/^[[:space:]]+/, "", n)
  sub(/[.[:space:]]+$/, "", n)
  # «&» всегда с одним пробелом с обеих сторон: «10AGE&Анет Сай», «A &B» -> «10AGE & Анет Сай», «A & B»
  # Ведущий и конечный «&» — часть имени («&ME»), его пробелами не окружаем
  lead = ""; tail = ""
  if (substr(n, 1, 1) == "&") { lead = "&"; n = substr(n, 2) }
  if (length(n) > 0 && substr(n, length(n), 1) == "&") { tail = "&"; n = substr(n, 1, length(n) - 1) }
  gsub(/[[:space:]]*&[[:space:]]*/, " \\& ", n)
  n = lead n tail
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
DEST_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    -n|--dry-run) DRY_RUN=1 ;;
    --dest)
      if [ $# -lt 2 ]; then
        echo "Ошибка: --dest требует каталог" >&2
        exit 2
      fi
      DEST_ARG="$2"
      shift
      ;;
    -h|--help)
      echo "Использование: ${SCRIPT_NAME} [-n|--dry-run] [--dest КАТАЛОГ] [каталог]"
      echo "  -n, --dry-run   только показать, что будет создано/перемещено/удалено, без изменений"
      echo "  --dest КАТАЛОГ  целевой каталог для результата; DEST_DIR из конфига применяется"
      echo "                  только при запуске без явного каталога (по списку из конфига)"
      exit 0
      ;;
    *) [ -z "${DIR_ARG}" ] && DIR_ARG="$1" ;;
  esac
  shift
done

# Официальные имена исполнителей (MusicBrainz): включается OFFICIAL_NAMES=yes в
# conf/split_by_dash.conf или переменной окружения. По умолчанию выключено, чтобы
# скрипт работал без сети.
OFFICIAL_NAMES="${OFFICIAL_NAMES:-no}"
OFFICIAL_API="${OFFICIAL_API:-https://musicbrainz.org/ws/2/artist}"
OFFICIAL_UA="mytasks-split_by_dash/1.0 (https://github.com/Eremiteb/mytasks)"
OFFICIAL_CACHE="${SCRIPT_DIR}/state/${SCRIPT_BASE}_official.tsv"

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
    | CANON="${canon}" LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e 'BEGIN { c = ENVIRON["CANON"]; w = tolower(c) } { n = strip_the(win_safe($0)) } tolower(n) == w && $0 != c')
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
        { s = strip_the(win_safe($0)); if (s == "") next
          k = nkey(s); if (k == "") next; n[k]++; v[k] = (k in v) ? v[k] "\n" $0 : $0; if (s != $0) dot[k] = 1 }
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

# Оценка «внешнего вида» имени: девять чисел через пробел —
# 1) 1, если имя целиком в верхнем регистре (HURTS), иначе 0;
# 2) 1, если первая буква не заглавная, иначе 0;
# 3) число заглавных букв, кроме первого символа;
# 4) 1, если в имени есть «_» или «+» (следы замены знаков при скачивании), иначе 0;
# 5) приоритет разделителя соавторов: 0 — « & », 1 — «&», 2 — «, », 3 — « и »/« and »,
#    4 — прочее;
# 6) 0, если в имени есть «ё» (Ёлка, Серёга), иначе 1;
# 7) 1, если есть латинская буква с диакритикой (Beyoncé), иначе 0;
# 8) 0, если есть казахская буква (Қайрат), иначе 1;
# 9) 0, если есть служебное слово (группа, тобы, ансамбль, band, official), иначе 1.
# Чем меньше значения, тем лучше имя.
case_rank() {
  printf '%s\n' "$1" | LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e '{
    n = length($0); up = 0; first_low = 1
    for (i = 1; i <= n; i++) {
      c = substr($0, i, 1)
      if (c != tolower(c) && c == toupper(c)) { if (i == 1) first_low = 0; else up++ }
    }
    all_caps = ($0 != tolower($0) && $0 == toupper($0)) ? 1 : 0
    art = ($0 ~ /[_+]/) ? 1 : 0
    if (index($0, " & ") > 0) sep = 0
    else if (index($0, "&") > 0) sep = 1
    else if (index($0, ", ") > 0) sep = 2
    else if ($0 ~ /[[:space:]](и|and)[[:space:]]/) sep = 3
    else sep = 4
    no_yo = has_yo($0) ? 0 : 1
    dia = has_dia($0)
    no_kz = has_kz($0) ? 0 : 1
    no_word = has_noise($0) ? 0 : 1
    print all_caps, first_low, up, art, sep, no_yo, dia, no_kz, no_word
  }'
}

# «Мягкий» ключ имени: нижний регистр без пробелов, «_», «+», «&», «,», апострофов,
# «!», «?» и точек в конце слов. Папки с равным мягким ключом отличаются только
# оформлением и объединяются без проверки; иначе (точки между буквами, дефисы:
# «G.A.M.E» / «Game», «Jay-Z» / «Jay z») слияние спорное и проверяется.
soft_key() {
  printf '%s\n' "$1" | LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e '{
    s = " " strip_the(foldchars(tolower($0))) " "
    gsub(/&amp;/, "\\&", s)
    s = drop_words(s)
    gsub(/\.+([[:space:]]|$)/, " ", s)
    gsub("[[:space:]_+&,\047`!?]", "", s)
    gsub("\342\200\231", "", s)
    gsub("\342\200\230", "", s)
    print s
  }'
}

# Печатает «trivial», если у всех имён из файла group_file одинаковый soft_key
group_is_trivial() {
  _gt_first=""
  _gt_trivial="trivial"
  while IFS= read -r _gt_name; do
    _gt_key=$(soft_key "${_gt_name}")
    if [ -z "${_gt_first}" ]; then
      _gt_first="${_gt_key}"
    elif [ "${_gt_key}" != "${_gt_first}" ]; then
      _gt_trivial="disputed"
    fi
  done < "$1"
  printf '%s\n' "${_gt_trivial}"
}

# Печатает уникальные теги исполнителя (artist, album_artist) до 5 аудиофайлов папки
member_tag_names() {
  command -v ffprobe >/dev/null 2>&1 || return 0
  _mt_list="$(mktemp)"
  find "$1" -type f \( -iname '*.mp3' -o -iname '*.flac' -o -iname '*.m4a' -o -iname '*.ogg' -o -iname '*.opus' -o -iname '*.wma' \) 2>/dev/null \
    | sort | head -n 5 > "${_mt_list}" || true
  while IFS= read -r _mt_file; do
    ffprobe -v error -show_entries format_tags=artist,album_artist -of default=nw=1:nk=1 "${_mt_file}" 2>/dev/null || true
  done < "${_mt_list}" | awk 'NF && !seen[$0]++'
  rm -f -- "${_mt_list}"
}

# Печатает «пары id<TAB>официальное_имя» артистов MusicBrainz, у которых имя или псевдоним
# в точности (без учёта регистра) равны name (оценка >= 90). Кэш — state/split_by_dash_mbid.tsv
# («имя<TAB>id<TAB>официальное_имя», «-» = не найдено); при сетевой ошибке кэш не пишется.
mb_ids() {
  _mi_cache="${SCRIPT_DIR}/state/${SCRIPT_BASE}_mbid.tsv"
  if [ -r "${_mi_cache}" ] && awk -F'\t' -v n="$1" '$1 == n { f = 1 } END { exit !f }' "${_mi_cache}"; then
    awk -F'\t' -v n="$1" '$1 == n && $2 != "-" { print $2 "\t" $3 }' "${_mi_cache}"
    return 0
  fi
  if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    return 0
  fi
  _mi_query=$(printf '%s' "$1" | tr '_' ' ')
  sleep 1
  if ! _mi_resp=$(curl -sf -m 20 -A "${OFFICIAL_UA}" -G "${OFFICIAL_API}" \
      --data-urlencode "query=artist:\"${_mi_query}\"" --data-urlencode "fmt=json" --data-urlencode "limit=5"); then
    log_json "WARN" "official_lookup_failed" "Не удалось запросить MusicBrainz" "${_mi_query}"
    return 0
  fi
  _mi_found=$(printf '%s' "${_mi_resp}" \
    | jq -r '.artists[]? | select(.score >= 90) | [.id, .name, ([.aliases[]?.name] | join("\u0001"))] | @tsv' 2>/dev/null \
    | LC_ALL=C.UTF-8 awk -F'\t' -v n="$1" 'BEGIN { w = tolower(n) } { ok = (tolower($2) == w); m = split($3, a, "\001"); for (i = 1; i <= m; i++) if (tolower(a[i]) == w) ok = 1; if (ok) print $1 "\t" $2 }' || true)
  mkdir -p "$(dirname -- "${_mi_cache}")"
  if [ -n "${_mi_found}" ]; then
    printf '%s\n' "${_mi_found}" | awk -F'\t' -v n="$1" '{ print n "\t" $1 "\t" $2 }' >> "${_mi_cache}"
  else
    printf '%s\t-\t-\n' "$1" >> "${_mi_cache}"
  fi
  printf '%s\n' "${_mi_found}" | awk 'NF'
}

# Общая часть проверки: по файлу с «строками member<TAB>значение» определяет, есть ли
# значение, общее для всех members_total участников. Печатает same / different / unknown:
# unknown — у кого-то из участников нет значений; different — у всех есть, общего нет.
common_verdict() {
  awk -F'\t' -v total="$2" -v with="$3" '
    BEGIN { if (with < total) { print "unknown"; exit } }
    { if (!(($1 SUBSEP $2) in seen)) { seen[$1 SUBSEP $2] = 1; c[$2]++ } }
    END { if (with < total) exit; for (k in c) if (c[k] == total) { print "same"; exit } print "different" }' "$1"
}

# Проверка спорной группы (имена папок — в файле group_file внутри parent). Порядок:
# 1) по файлам: общий тег исполнителя во всех папках => слияние подтверждено;
# 2) по официальным источникам (MusicBrainz, если OFFICIAL_NAMES=yes): у всех папок
#    (по имени папки и тегам) есть общий артист => слияние подтверждено (имя артиста
#    становится именем папки), у всех есть, но общего нет => отклонено;
# 3) не удалось подтвердить => слияние отклоняется (разбирается вручную).
# Решение кэшируется в state/split_by_dash_verdicts.tsv («ключ<TAB>merge|keep<TAB>причина»);
# файл можно править вручную, чтобы принудительно объединить или не объединять группу.
# Результат — переменные VERDICT (merge|keep) и VERIFIED_NAME (официальное имя или пусто).
verify_group() {
  VERDICT="merge"
  VERIFIED_NAME=""
  _vg_parent="$1"
  _vg_group="$2"
  _vg_trivial=$(group_is_trivial "${_vg_group}")
  [ "${_vg_trivial}" = "trivial" ] && return 0

  _vg_first=$(sed -n '1p' "${_vg_group}")
  _vg_key=$(name_key "${_vg_first}")
  _vg_members=$(wc -l < "${_vg_group}")
  _vg_cache="${SCRIPT_DIR}/state/${SCRIPT_BASE}_verdicts.tsv"
  if [ -r "${_vg_cache}" ]; then
    _vg_hit=$(awk -F'\t' -v k="${_vg_key}" '$1 == k { print $2 "\t" $3; exit }' "${_vg_cache}")
    if [ -n "${_vg_hit}" ]; then
      VERDICT="${_vg_hit%%	*}"
      VERIFIED_NAME=""
      return 0
    fi
  fi

  # --- 1. файлы
  _vg_files="$(mktemp)"
  _vg_with=0
  _vg_idx=0
  while IFS= read -r _vg_member; do
    _vg_idx=$((_vg_idx+1))
    _vg_tags=$(member_tag_names "${_vg_parent}/${_vg_member}")
    if [ -n "${_vg_tags}" ]; then
      _vg_with=$((_vg_with+1))
      while IFS= read -r _vg_tag; do
        _vg_tag_key=$(name_key "${_vg_tag}")
        [ -z "${_vg_tag_key}" ] || printf '%s\t%s\n' "${_vg_idx}" "${_vg_tag_key}" >> "${_vg_files}"
      done <<VG_TAGS_EOF
${_vg_tags}
VG_TAGS_EOF
    fi
  done < "${_vg_group}"
  _vg_files_verdict=$(common_verdict "${_vg_files}" "${_vg_members}" "${_vg_with}")
  : > "${_vg_files}"

  _vg_reason="файлы: ${_vg_files_verdict}"
  if [ "${_vg_files_verdict}" = "same" ]; then
    VERDICT="merge"
  else
    # --- 2. официальные источники
    _vg_off_verdict="unknown"
    if [ "${OFFICIAL_NAMES}" = "yes" ]; then
      _vg_with=0
      _vg_idx=0
      : > "${_vg_files}"
      _vg_names_file="$(mktemp)"
      while IFS= read -r _vg_member; do
        _vg_idx=$((_vg_idx+1))
        { printf '%s\n' "${_vg_member}"; member_tag_names "${_vg_parent}/${_vg_member}" | head -n 2; } > "${_vg_names_file}"
        _vg_got=0
        while IFS= read -r _vg_name; do
          [ -n "${_vg_name}" ] || continue
          _vg_ids=$(mb_ids "${_vg_name}")
          if [ -n "${_vg_ids}" ]; then
            _vg_got=1
            printf '%s\n' "${_vg_ids}" | awk -F'\t' -v i="${_vg_idx}" '{ print i "\t" $1 "\t" $2 }' >> "${_vg_files}"
          fi
        done < "${_vg_names_file}"
        _vg_with=$((_vg_with+_vg_got))
      done < "${_vg_group}"
      rm -f -- "${_vg_names_file}"
      # значения для сравнения — id артиста (2-й столбец); имя хранится в 3-м
      _vg_ids_file="$(mktemp)"
      awk -F'\t' '{ print $1 "\t" $2 }' "${_vg_files}" > "${_vg_ids_file}"
      _vg_off_verdict=$(common_verdict "${_vg_ids_file}" "${_vg_members}" "${_vg_with}")
      if [ "${_vg_off_verdict}" = "same" ]; then
        _vg_common=$(awk -F'\t' -v total="${_vg_members}" '{ if (!(($1 SUBSEP $2) in s)) { s[$1 SUBSEP $2] = 1; c[$2]++ } } END { for (k in c) if (c[k] == total) { print k; exit } }' "${_vg_ids_file}")
        VERIFIED_NAME=$(awk -F'\t' -v id="${_vg_common}" '$2 == id { print $3; exit }' "${_vg_files}")
      fi
      rm -f -- "${_vg_ids_file}"
    fi
    _vg_reason="${_vg_reason}; MusicBrainz: ${_vg_off_verdict}"
    if [ "${_vg_off_verdict}" = "same" ]; then
      VERDICT="merge"
    else
      VERDICT="keep"
    fi
  fi
  rm -f -- "${_vg_files}"

  mkdir -p "$(dirname -- "${_vg_cache}")"
  printf '%s\t%s\t%s\n' "${_vg_key}" "${VERDICT}" "${_vg_reason}" >> "${_vg_cache}"
  _vg_list=$(tr '\n' '|' < "${_vg_group}")
  if [ "${VERDICT}" = "keep" ]; then
    echo "Внимание: спорное слияние отклонено (${_vg_reason}); проверьте вручную: ${_vg_list}"
    log_json "WARN" "merge_rejected" "Спорное слияние отклонено: не подтверждено файлами и официальными источниками" "${_vg_reason}; ${_vg_list}"
  else
    log_json "INFO" "merge_verified" "Спорное слияние подтверждено" "${_vg_reason}; ${VERIFIED_NAME}"
  fi
}

# Объединяет одну группу папок (имена в файле group_file) внутри parent в одну.
# Группа — папки с одинаковым name_key (различия только в регистре, знаках, пробелах,
# «&», апострофах, диакритике, ё/е). Имя результата: официальное (MusicBrainz, если OFFICIAL_NAMES=yes
# и найдено; папка создаётся, даже если среди вариантов её нет), иначе лучший вариант:
# 1) не «КАПСОМ» целиком; 2) без «_»/«+»; 3) приоритет «&» (« & » > «&» > «, » > прочее);
# 3a) вариант со служебным словом («Жігіттер тобы», «Группа губы») — независимо от числа файлов;
# 4) вариант с «ё» (Ёлка, Серёга) — независимо от числа файлов; 5) вариант с казахскими
# буквами (Қайрат, а не Кайрат) — независимо от числа файлов; 6) вариант без латинской
# диакритики (Beyonce, а не Beyoncé); 7) больше файлов; 8) ближе к «Первая заглавная,
# остальные строчные» (первая буква заглавная, меньше прочих заглавных); 9) первая по
# кодовым точкам.
# Группа из одной папки с недопустимым для Windows именем просто переименовывается.
merge_case_group() {
  parent="$1"
  group_file="$2"
  verify_group "${parent}" "${group_file}"
  if [ "${VERDICT}" = "keep" ]; then
    return 0
  fi
  best=""
  best_n=-1
  best_caps=0
  best_low=0
  best_up=0
  best_art=0
  best_sep=0
  best_plain=0
  best_dia=0
  best_nokz=0
  best_noise=0
  while IFS= read -r member; do
    member_n=$(find "${parent}/${member}" -type f | wc -l)
    member_clean=$(folder_name "${member}")
    member_rank=$(case_rank "${member_clean}")
    read -r m_caps m_low m_up m_art m_sep m_plain m_dia m_nokz m_noise <<RANK_EOF
${member_rank}
RANK_EOF
    better=0
    if [ "${best_n}" -lt 0 ]; then
      better=1
    elif [ "${m_caps}" -ne "${best_caps}" ]; then
      [ "${m_caps}" -lt "${best_caps}" ] && better=1
    elif [ "${m_art}" -ne "${best_art}" ]; then
      [ "${m_art}" -lt "${best_art}" ] && better=1
    elif [ "${m_sep}" -ne "${best_sep}" ]; then
      [ "${m_sep}" -lt "${best_sep}" ] && better=1
    elif [ "${m_noise}" -ne "${best_noise}" ]; then
      [ "${m_noise}" -lt "${best_noise}" ] && better=1
    elif [ "${m_plain}" -ne "${best_plain}" ]; then
      [ "${m_plain}" -lt "${best_plain}" ] && better=1
    elif [ "${m_nokz}" -ne "${best_nokz}" ]; then
      [ "${m_nokz}" -lt "${best_nokz}" ] && better=1
    elif [ "${m_dia}" -ne "${best_dia}" ]; then
      [ "${m_dia}" -lt "${best_dia}" ] && better=1
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
      best_art="${m_art}"
      best_sep="${m_sep}"
      best_plain="${m_plain}"
      best_dia="${m_dia}"
      best_nokz="${m_nokz}"
      best_noise="${m_noise}"
    fi
  done < "${group_file}"

  if [ -n "${VERIFIED_NAME}" ]; then
    verified_safe=$(printf '%s' "${VERIFIED_NAME}" | tr '/' '_')
    verified_safe=$(folder_name "${verified_safe}")
    if [ -n "${verified_safe}" ] && [ "${verified_safe}" != "${best}" ]; then
      log_action "INFO" "official_name" "Использовано имя артиста, подтверждённое MusicBrainz" "${best} -> ${verified_safe}"
      best="${verified_safe}"
    fi
  elif [ "${OFFICIAL_NAMES}" = "yes" ]; then
    best_key=$(name_key "${best}")
    group_names=$(cat "${group_file}")
    official=$(official_name "${best_key}" "${best}
${group_names}")
    if [ -n "${official}" ]; then
      official_safe=$(printf '%s' "${official}" | tr '/' '_')
      official_safe=$(folder_name "${official_safe}")
      if [ -n "${official_safe}" ] && [ "${official_safe}" != "${best}" ]; then
        log_action "INFO" "official_name" "Использовано официальное имя исполнителя (MusicBrainz)" "${best} -> ${official_safe}"
        best="${official_safe}"
      fi
    fi
  fi

  best_dir="${parent}/${best}"
  if [ ! -d "${best_dir}" ]; then
    do_mkdir "${best_dir}"
    log_action "INFO" "mkdir" "Создана папка эталонного имени" "${best_dir}"
  fi

  while IFS= read -r member; do
    [ "${member}" = "${best}" ] && continue
    merge_dir_into "${parent}/${member}" "${best_dir}"
    if [ "${DRY_RUN}" -eq 0 ] && [ -d "${parent}/${member}" ]; then
      log_json "ERROR" "case_merge_failed" "Не удалось удалить папку-дубликат" "${parent}/${member}"
    else
      log_action "INFO" "case_merged" "Папка объединена с эталонной" "${parent}/${member} -> ${best_dir}"
    fi
  done < "${group_file}"
}

# Приводит имя к допустимому в Windows (см. win_safe в начале файла).
win_safe_name() {
  printf '%s\n' "$1" | LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e '{ print win_safe($0) }'
}

# Имя папки исполнителя: допустимое в Windows и без ведущего «The »
folder_name() {
  printf '%s\n' "$1" | LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e '{ print strip_the(win_safe($0)) }'
}

# Ключ имени для сравнения папок: нижний регистр, только буквы и цифры (знаки
# препинания, пробелы, «_», «&», апострофы, слова «и»/«and» отброшены), как
# normalize_db_text в music_downloader: «10AGE Анет Сай» == «10AGE&Анет Сай», «Банд'Эрос» == «БандЭрос».
name_key() {
  printf '%s\n' "$1" | LC_ALL=C.UTF-8 awk -f "${WIN_SAFE_FILE}" -e '{ print nkey($0) }'
}

# Печатает официальное имя исполнителя по ключу key (name_key) из MusicBrainz или
# ничего, если оно не найдено. Искомые варианты написания — в $2 (по одному в строке);
# перебираются до 3 разных вариантов. Принимается только артист с оценкой >= 90, чьё
# официальное имя даёт тот же ключ. Результаты (и отрицательные, «-») кэшируются в
# state/split_by_dash_official.tsv (строка «ключ<TAB>имя»; файл можно править вручную);
# при сетевой ошибке запись в кэш не делается. Запросы — не чаще раза в секунду.
official_name() {
  _on_key="$1"
  if [ -r "${OFFICIAL_CACHE}" ]; then
    _on_hit=$(awk -F'\t' -v k="${_on_key}" '$1 == k { print $2; exit }' "${OFFICIAL_CACHE}")
    if [ -n "${_on_hit}" ]; then
      [ "${_on_hit}" = "-" ] || printf '%s\n' "${_on_hit}"
      return 0
    fi
  fi
  if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    log_json "WARN" "official_unavailable" "Для поиска официальных имён нужны curl и jq" "${_on_key}"
    return 0
  fi
  _on_found=""
  _on_tried=0
  _on_seen=""
  while IFS= read -r _on_variant; do
    [ -n "${_on_variant}" ] || continue
    _on_query=$(printf '%s' "${_on_variant}" | tr '_' ' ')
    case "${_on_seen}" in *"|${_on_query}|"*) continue ;; *) ;; esac
    _on_seen="${_on_seen}|${_on_query}|"
    [ "${_on_tried}" -lt 3 ] || break
    _on_tried=$((_on_tried+1))
    sleep 1
    if ! _on_resp=$(curl -sf -m 20 -A "${OFFICIAL_UA}" -G "${OFFICIAL_API}" \
        --data-urlencode "query=artist:\"${_on_query}\"" --data-urlencode "fmt=json" --data-urlencode "limit=5"); then
      log_json "WARN" "official_lookup_failed" "Не удалось запросить официальное имя" "${_on_query}"
      return 0
    fi
    _on_names=$(printf '%s' "${_on_resp}" | jq -r '.artists[]? | select(.score >= 90) | .name' 2>/dev/null || true)
    while IFS= read -r _on_name; do
      [ -n "${_on_name}" ] || continue
      _on_name_key=$(name_key "${_on_name}")
      if [ "${_on_name_key}" = "${_on_key}" ]; then
        _on_found="${_on_name}"
        break
      fi
    done <<ON_EOF
${_on_names}
ON_EOF
    [ -z "${_on_found}" ] || break
  done <<ON_VARIANTS_EOF
$2
ON_VARIANTS_EOF
  mkdir -p "$(dirname -- "${OFFICIAL_CACHE}")"
  printf '%s\t%s\n' "${_on_key}" "${_on_found:--}" >> "${OFFICIAL_CACHE}"
  [ -z "${_on_found}" ] || printf '%s\n' "${_on_found}"
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
    folder_full=$(win_safe_name "${folder_raw}")
    folder=$(folder_name "${folder_raw}")
    [ -z "${folder}" ] && continue
    rest="${name#"${folder_raw}"}"
    new_name=$(win_safe_name "${folder_full}${rest}")
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
  conf_official=$(sed -n 's/^[[:space:]]*OFFICIAL_NAMES[[:space:]]*=[[:space:]]*//p' "${CONFIG_FILE}" | sed 's/#.*//' | tail -n1)
  conf_official=$(trim "${conf_official}")
  [ -z "${conf_official}" ] || OFFICIAL_NAMES="${conf_official}"
fi

# При явно указанном каталоге DEST_DIR из конфига не применяется (иначе проход слияния и
# переименования затрагивал бы целевой каталог из боевого конфига, например при
# отладке на временной папке): целевой каталог задаётся только ключом --dest.
if [ -n "${DIR_ARG}" ]; then
  DEST_DIR="${DEST_ARG}"
  if [ ! -d "${DIR_ARG}" ]; then
    echo "Ошибка: каталог не существует: ${DIR_ARG}" >&2
    log_json "ERROR" "dir_missing" "Указанный каталог не существует" "${DIR_ARG}"
    cleanup_logs
    exit 2
  fi
fi

if [ -n "${DIR_ARG}" ] && [ -d "${DIR_ARG}" ]; then
  process_directory "${DIR_ARG}"
elif [ -r "${CONFIG_FILE}" ]; then
  log_json "INFO" "config_used" "Использование конфига" "${CONFIG_FILE}"
  while IFS= read -r line || [ -n "${line}" ]; do
    case "${line}" in ""|\#*) continue ;; *) ;; esac
    case "${line}" in *DEST_DIR=*|*OFFICIAL_NAMES=*) continue ;; *) ;; esac
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