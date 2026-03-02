#!/usr/bin/env bash
# doc2fb2.sh — DOC/DOCX/RTF/ODT -> FB2 (recursive), SINGLE-THREAD
# JSONL log (real actions + errors), progress per line (+percent), log-level filtering

set -u

usage() {
cat <<'EOF'
doc2fb2.sh — конвертация DOC/DOCX/RTF/ODT -> FB2 (рекурсивно), ОДНОПОТОЧНО

Прогресс:
  Одна строка на каждый найденный файл:
    [i/total | P%] OK|DRY|SKIP|FAIL  src -> out  (detail)

Лог (JSONL):
  По умолчанию пишем реальные действия и ошибки.
  Пропуски (SKIP/exists/skip-newer) и dry-run — в лог НЕ пишутся.
  Можно ограничить лог только ошибками: --log-level error

Использование:
  doc2fb2.sh -d DIR [опции]

Опции:
  -d, --dir DIR
  -o, --out OUTDIR
  -l, --log LOGFILE
  -n, --dry-run
      --overwrite
      --skip-newer              (только вместе с --overwrite)
      --exclude PATTERN         (можно несколько раз; сравнение с basename)
      --only doc|docx|rtf|odt|all
      --author NAME
      --title-mode auto|filename|keep
      --toc / --no-toc
      --toc-depth N
      --log-level info|error    (по умолчанию: info)
      --debug
  -h, --help

Pipeline:
  docx -> pandoc -> fb2
  odt  -> pandoc -> fb2
  doc/rtf -> LibreOffice -> odt -> pandoc -> fb2
  Если LO не смог загрузить документ — fallback:
    - rtf: pandoc напрямую; затем unrtf -> txt -> pandoc (если unrtf есть)
    - doc: если mime=text/html -> pandoc html -> fb2
           иначе antiword/catdoc/catdoc -b/wvText -> txt -> pandoc (что доступно)

Автоудаление логов:
  Удаляет doc2fb2_*.jsonl старше 10 дней рядом с логом.
EOF
}

ts() { date '+%Y-%m-%d %H:%M:%S'; }

json_escape() {
  awk 'BEGIN{ORS=""; first=1}
  {
    if(!first) printf "\\n";
    first=0;
    gsub(/\\/,"\\\\");
    gsub(/"/,"\\\"");
    gsub(/\t/,"\\t");
    gsub(/\r/,"\\r");
    printf "%s",$0;
  }'
}

log_json() {
  local level="$1" event="$2" src="${3:-}" out="${4:-}" msg="${5:-}" detail="${6:-}"
  printf '{"ts":"%s","level":"%s","event":"%s","src":"%s","out":"%s","msg":"%s","detail":"%s","pid":%s}\n' \
    "$(ts)" "$level" "$event" \
    "$(printf '%s' "$src" | json_escape)" \
    "$(printf '%s' "$out" | json_escape)" \
    "$(printf '%s' "$msg" | json_escape)" \
    "$(printf '%s' "$detail" | json_escape)" \
    "$$" >> "$LOGFILE"
}

# log-level filtering:
# LOG_LEVEL=info -> writes info + error
# LOG_LEVEL=error -> writes only error
log_info() {
  [ "${LOG_LEVEL:-info}" = "error" ] && return 0
  log_json "info" "$1" "${2:-}" "${3:-}" "${4:-}" "${5:-}"
}
log_error() {
  log_json "error" "$1" "${2:-}" "${3:-}" "${4:-}" "${5:-}"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

###############################################################################
# Args / defaults
###############################################################################
DIR=""
OUTDIR=""
LOGFILE=""
DRYRUN=0
OVERWRITE=0
SKIP_NEWER=0

AUTHOR_SET=0
AUTHOR="${USER:-}"
TITLE_MODE="auto"

TOC=1
TOC_DEPTH=3

ONLY="all"
EXCLUDES=()

LOG_LEVEL="info"   # info|error
DEBUG=0

ORIG_ARGS=("$@")

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --debug) DEBUG=1; shift ;;
    -d|--dir) DIR="${2:-}"; shift 2 ;;
    -o|--out) OUTDIR="${2:-}"; shift 2 ;;
    -l|--log) LOGFILE="${2:-}"; shift 2 ;;
    -n|--dry-run) DRYRUN=1; shift ;;
    --overwrite) OVERWRITE=1; shift ;;
    --skip-newer) SKIP_NEWER=1; shift ;;
    --author) AUTHOR_SET=1; AUTHOR="${2:-}"; shift 2 ;;
    --title-mode) TITLE_MODE="${2:-}"; shift 2 ;;
    --toc) TOC=1; shift ;;
    --no-toc) TOC=0; shift ;;
    --toc-depth) TOC_DEPTH="${2:-}"; shift 2 ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    --exclude) EXCLUDES+=( "${2:-}" ); shift 2 ;;
    --log-level) LOG_LEVEL="${2:-}"; shift 2 ;;
    --) shift; break ;;
    -*) echo "Неизвестная опция: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done

if [ "$DEBUG" -eq 1 ]; then
  echo "DEBUG:"
  echo "  script: $0"
  echo "  orig args count: ${#ORIG_ARGS[@]}"
  for i in "${!ORIG_ARGS[@]}"; do printf '  arg[%d]=<%s>\n' "$i" "${ORIG_ARGS[$i]}"; done
  echo "  parsed DIR=<${DIR}>"
  echo "  parsed OUTDIR=<${OUTDIR}>"
  echo "  parsed LOGFILE=<${LOGFILE}>"
  echo "  DRYRUN=$DRYRUN OVERWRITE=$OVERWRITE SKIP_NEWER=$SKIP_NEWER"
  echo "  ONLY=$ONLY TITLE_MODE=$TITLE_MODE TOC=$TOC TOC_DEPTH=$TOC_DEPTH"
  echo "  LOG_LEVEL=$LOG_LEVEL"
  echo "  EXCLUDES count=${#EXCLUDES[@]}"
  exit 0
fi

###############################################################################
# Validate / deps
###############################################################################
[ -n "${DIR:-}" ] || { echo "Ошибка: не указан -d/--dir" >&2; usage; exit 2; }
[ -d "$DIR" ] || { echo "Ошибка: DIR не каталог: $DIR" >&2; exit 2; }

case "$ONLY" in all|doc|docx|rtf|odt) ;; *) echo "Ошибка: --only all|doc|docx|rtf|odt" >&2; exit 2 ;; esac
if [ "$SKIP_NEWER" -eq 1 ] && [ "$OVERWRITE" -eq 0 ]; then
  echo "Ошибка: --skip-newer работает только вместе с --overwrite" >&2
  exit 2
fi

case "$LOG_LEVEL" in
  info|error) ;;
  *) echo "Ошибка: --log-level должен быть info или error" >&2; exit 2 ;;
esac

have_cmd find   || { echo "Ошибка: find не найден" >&2; exit 3; }
have_cmd pandoc || { echo "Ошибка: pandoc не найден" >&2; exit 3; }
have_cmd awk    || { echo "Ошибка: awk не найден" >&2; exit 3; }
have_cmd unzip  || { echo "Ошибка: unzip не найден (нужен для метаданных docx)" >&2; exit 3; }

if have_cmd soffice; then LO_CMD="soffice"
elif have_cmd libreoffice; then LO_CMD="libreoffice"
else LO_CMD=""
fi

DIR="$(cd "$DIR" 2>/dev/null && pwd)" || { echo "Ошибка: не могу открыть каталог: $DIR" >&2; exit 2; }

if [ -n "${OUTDIR:-}" ]; then
  mkdir -p "$OUTDIR" 2>/dev/null || { echo "Ошибка: не могу создать OUTDIR: $OUTDIR" >&2; exit 2; }
  OUTDIR="$(cd "$OUTDIR" 2>/dev/null && pwd)" || { echo "Ошибка: не могу открыть OUTDIR: $OUTDIR" >&2; exit 2; }
fi

if [ -z "${LOGFILE:-}" ]; then
  LOGFILE="./doc2fb2_$(date '+%Y-%m-%d').jsonl"
fi
mkdir -p "$(dirname "$LOGFILE")" 2>/dev/null || { echo "Ошибка: не могу создать каталог лога" >&2; exit 2; }

###############################################################################
# Auto-delete old logs (keep 10 days)
###############################################################################
cleanup_old_logs() {
  local keep_days=10
  local logdir cur pattern
  logdir="$(cd "$(dirname "$LOGFILE")" 2>/dev/null && pwd)" || return 0
  cur="$logdir/$(basename "$LOGFILE")"
  pattern="doc2fb2_*.jsonl"

  log_info "log_cleanup_start" "" "" "Очистка логов старше 10 дней" "dir=$logdir pattern=$pattern keep_days=$keep_days"

  find "$logdir" -maxdepth 1 -type f -name "$pattern" -mtime +"$keep_days" -print0 2>/dev/null |
  while IFS= read -r -d '' f; do
    [ "$f" = "$cur" ] && continue
    if rm -f -- "$f" 2>/dev/null; then
      log_info "log_cleanup_deleted" "$f" "" "Удалён старый лог" "keep_days=$keep_days"
    else
      log_error "log_cleanup_failed" "$f" "" "Не удалось удалить старый лог" "keep_days=$keep_days"
    fi
  done

  log_info "log_cleanup_done" "" "" "Очистка логов завершена" ""
}

cleanup_old_logs

###############################################################################
# Helpers
###############################################################################
matches_excludes() {
  local bn="$1" p
  for p in "${EXCLUDES[@]}"; do
    [[ "$bn" == $p ]] && return 0
  done
  return 1
}

only_allows_ext() {
  local ext="${1,,}"
  [ "$ONLY" = "all" ] && return 0
  [[ "$ext" == "$ONLY" ]]
}

title_from_filename() {
  local f="$1" b name
  b="$(basename "$f")"
  name="${b%.*}"
  name="${name//_/ }"
  name="${name//-/ }"
  name="$(printf '%s' "$name" | awk '{$1=$1;print}')"
  printf '%s' "$name"
}

extract_docx_meta() {
  local docx="$1"
  unzip -p "$docx" docProps/core.xml 2>/dev/null | awk '
    BEGIN{ s=""; }
    { s = s $0 "\n"; }
    END{
      title=""; creator="";
      if (match(s, /<dc:title[^>]*>([^<]*)<\/dc:title>/, m)) title=m[1];
      if (match(s, /<dc:creator[^>]*>([^<]*)<\/dc:creator>/, n)) creator=n[1];
      gsub(/^[ \t\r\n]+/, "", title); gsub(/[ \t\r\n]+$/, "", title);
      gsub(/^[ \t\r\n]+/, "", creator); gsub(/[ \t\r\n]+$/, "", creator);
      printf "%s\t%s", title, creator;
    }'
}

make_out_path() {
  local src="$1" base name rel rel_dir
  base="$(basename "$src")"
  name="${base%.*}"
  if [ -n "${OUTDIR:-}" ] && [[ "$src" == "$DIR/"* ]]; then
    rel="${src#"$DIR"/}"
    rel_dir="$(dirname "$rel")"
    printf '%s/%s/%s.fb2\n' "$OUTDIR" "$rel_dir" "$name"
  elif [ -n "${OUTDIR:-}" ]; then
    printf '%s/%s.fb2\n' "$OUTDIR" "$name"
  else
    printf '%s/%s.fb2\n' "$(dirname "$src")" "$name"
  fi
}

ensure_outdir_for() { mkdir -p "$(dirname "$1")" 2>/dev/null; }

url_escape_path() {
  local s="$1"
  s="${s//%/%25}"
  s="${s//#/%23}"
  s="${s// /%20}"
  printf '%s' "$s"
}

lo_has_error() { grep -q '^Error:' "$1" 2>/dev/null; }

get_mime() {
  if have_cmd file; then
    file -b --mime-type -- "$1" 2>/dev/null || true
  else
    printf ''
  fi
}

run_pandoc_to_fb2() {
  local in="$1" fmt="$2" src="$3" out="$4" errfile="$5"
  local -a meta toc fmtarg
  meta=(); toc=(); fmtarg=()
  [ "$TOC" -eq 1 ] && toc+=( "--toc" "--toc-depth=${TOC_DEPTH}" )
  [ -n "$fmt" ] && fmtarg=( -f "$fmt" )

  local meta_title="" meta_creator="" ttitle="" tauthor=""

  if [[ "${in,,}" == *.docx ]]; then
    IFS=$'\t' read -r meta_title meta_creator < <(extract_docx_meta "$in")
  fi

  case "$TITLE_MODE" in
    auto)
      if [ -n "$meta_title" ]; then ttitle="$meta_title"; else ttitle="$(title_from_filename "$src")"; fi
      meta+=( "--metadata" "title=$ttitle" )
      ;;
    filename)
      ttitle="$(title_from_filename "$src")"
      meta+=( "--metadata" "title=$ttitle" )
      ;;
    keep) ;;
    *)
      log_error "stage_failed" "$src" "$out" "Некорректный --title-mode" "value=$TITLE_MODE"
      return 2
      ;;
  esac

  if [ "$AUTHOR_SET" -eq 1 ]; then
    tauthor="$AUTHOR"
  else
    if [ -n "$meta_creator" ]; then tauthor="$meta_creator"
    elif [ -n "${USER:-}" ]; then tauthor="$USER"
    else tauthor=""
    fi
  fi
  [ -n "$tauthor" ] && meta+=( "--metadata" "author=$tauthor" )

  if pandoc "${fmtarg[@]}" "$in" -o "$out" "${meta[@]}" "${toc[@]}" >>"$errfile" 2>&1; then
    log_info "ok" "$src" "$out" "Успешно" ""
    return 0
  else
    rm -f "$out" >/dev/null 2>&1 || true
    log_error "pandoc_failed" "$src" "$out" "Ошибка pandoc" "tail_err=$(tail -n 240 "$errfile" 2>/dev/null || true)"
    return 1
  fi
}

fallback_convert() {
  local src="$1" out="$2" ext="$3" workdir="$4" errfile="$5"
  local mime txt

  mime="$(get_mime "$src")"
  log_info "fallback_start" "$src" "$out" "Пробуем fallback без LibreOffice" "ext=${ext,,} mime=${mime:-unknown}"

  if [[ "${ext,,}" == "rtf" ]]; then
    run_pandoc_to_fb2 "$src" "" "$src" "$out" "$errfile" && return 0
    if have_cmd unrtf; then
      txt="$workdir/fallback.txt"
      if unrtf --text --nopict --quiet -- "$src" >"$txt" 2>>"$errfile"; then
        run_pandoc_to_fb2 "$txt" "plain" "$src" "$out" "$errfile" && return 0
      fi
    fi
    log_error "fallback_failed" "$src" "$out" "Fallback не помог для RTF" "mime=${mime:-unknown}; tail_err=$(tail -n 120 "$errfile" 2>/dev/null || true)"
    return 1
  fi

  if [[ "${ext,,}" == "doc" ]]; then
    if [[ "$mime" == "text/html" || "$mime" == "application/xhtml+xml" ]]; then
      run_pandoc_to_fb2 "$src" "html" "$src" "$out" "$errfile" && return 0
    fi

    txt="$workdir/fallback.txt"

    if have_cmd antiword; then
      if antiword "$src" >"$txt" 2>>"$errfile"; then
        run_pandoc_to_fb2 "$txt" "plain" "$src" "$out" "$errfile" && return 0
      fi
    fi

    if have_cmd catdoc; then
      if catdoc "$src" >"$txt" 2>>"$errfile"; then
        run_pandoc_to_fb2 "$txt" "plain" "$src" "$out" "$errfile" && return 0
      fi
      if catdoc -b "$src" >"$txt" 2>>"$errfile"; then
        run_pandoc_to_fb2 "$txt" "plain" "$src" "$out" "$errfile" && return 0
      fi
    fi

    if have_cmd wvText; then
      if wvText "$src" "$txt" >>"$errfile" 2>&1; then
        [ -s "$txt" ] && run_pandoc_to_fb2 "$txt" "plain" "$src" "$out" "$errfile" && return 0
      fi
    fi

    log_error "fallback_failed" "$src" "$out" "Fallback не помог для DOC" \
      "mime=${mime:-unknown}; have_antiword=$(have_cmd antiword && echo 1 || echo 0); have_catdoc=$(have_cmd catdoc && echo 1 || echo 0); have_wvText=$(have_cmd wvText && echo 1 || echo 0); tail_err=$(tail -n 200 "$errfile" 2>/dev/null || true)"
    return 1
  fi

  log_error "fallback_failed" "$src" "$out" "Fallback не настроен для этого типа" "ext=${ext,,} mime=${mime:-unknown}"
  return 1
}

short_fail_reason() {
  local errfile="$1"
  local s
  s="$(grep -m1 '^Error:' "$errfile" 2>/dev/null || true)"
  [ -n "$s" ] && printf '%s' "$s" && return 0
  tail -n 1 "$errfile" 2>/dev/null || true
}

convert_one() {
  # prints: STATUS|DETAIL|SRC|OUT
  local src="$1"
  local base ext out
  local tmproot workdir errfile staged_docx staged_odt produced produced2
  local detail=""

  base="$(basename "$src")"
  ext="${src##*.}"
  out="$(make_out_path "$src")"

  if matches_excludes "$base"; then echo "SKIP|excluded|$src|$out"; return 0; fi
  if ! only_allows_ext "$ext"; then echo "SKIP|filtered|$src|$out"; return 0; fi
  case "$base" in "~$"*) echo "SKIP|tempfile|$src|$out"; return 0 ;; esac

  if [ -e "$out" ]; then
    if [ "$OVERWRITE" -eq 0 ]; then echo "SKIP|exists|$src|$out"; return 0; fi
    if [ "$SKIP_NEWER" -eq 1 ] && ! [ "$src" -nt "$out" ]; then echo "SKIP|skip-newer|$src|$out"; return 0; fi
  fi

  if [ "$DRYRUN" -eq 1 ]; then echo "DRY||$src|$out"; return 0; fi

  ensure_outdir_for "$out"

  tmproot="${TMPDIR:-/tmp}/doc2fb2.$$.$RANDOM"
  workdir="$tmproot/work"
  mkdir -p "$workdir" 2>/dev/null || {
    log_error "stage_failed" "$src" "$out" "Не удалось создать временный каталог" "tmp=$workdir"
    echo "FAIL|tmpdir|$src|$out"
    return 0
  }
  errfile="$workdir/err.txt"; : > "$errfile"

  staged_docx="$workdir/staged.docx"
  staged_odt="$workdir/staged.odt"

  local lo_profile="$tmproot/lo_profile"
  mkdir -p "$lo_profile" 2>/dev/null || true
  local lo_uri="file://$(url_escape_path "$lo_profile")"

  if [[ "${ext,,}" == "docx" ]]; then
    cp -f "$src" "$staged_docx" >>"$errfile" 2>&1 || {
      log_error "stage_failed" "$src" "$out" "Не удалось подготовить .docx" "tail_err=$(tail -n 120 "$errfile" 2>/dev/null || true)"
      echo "FAIL|stage-docx|$src|$out"
      rm -rf "$tmproot" >/dev/null 2>&1 || true
      return 0
    }
    log_info "docx_to_fb2_start" "$src" "$out" "docx -> fb2 (pandoc)" ""
    if run_pandoc_to_fb2 "$staged_docx" "" "$src" "$out" "$errfile"; then
      echo "OK||$src|$out"
    else
      detail="$(short_fail_reason "$errfile")"
      echo "FAIL|pandoc:${detail:0:140}|$src|$out"
    fi
    rm -rf "$tmproot" >/dev/null 2>&1 || true
    return 0
  fi

  if [[ "${ext,,}" == "odt" ]]; then
    cp -f "$src" "$staged_odt" >>"$errfile" 2>&1 || {
      log_error "stage_failed" "$src" "$out" "Не удалось подготовить .odt" "tail_err=$(tail -n 120 "$errfile" 2>/dev/null || true)"
      echo "FAIL|stage-odt|$src|$out"
      rm -rf "$tmproot" >/dev/null 2>&1 || true
      return 0
    }
    log_info "odt_to_fb2_start" "$src" "$out" "odt -> fb2 (pandoc)" ""
    if run_pandoc_to_fb2 "$staged_odt" "" "$src" "$out" "$errfile"; then
      echo "OK||$src|$out"
    else
      detail="$(short_fail_reason "$errfile")"
      echo "FAIL|pandoc:${detail:0:140}|$src|$out"
    fi
    rm -rf "$tmproot" >/dev/null 2>&1 || true
    return 0
  fi

  if [[ "${ext,,}" == "doc" || "${ext,,}" == "rtf" ]]; then
    if [ -z "${LO_CMD:-}" ]; then
      log_error "to_odt_failed" "$src" "$out" "LibreOffice не найден" "rc=127"
      if fallback_convert "$src" "$out" "$ext" "$workdir" "$errfile"; then
        echo "OK|fallback|$src|$out"
      else
        detail="$(short_fail_reason "$errfile")"
        echo "FAIL|fallback:${detail:0:140}|$src|$out"
      fi
      rm -rf "$tmproot" >/dev/null 2>&1 || true
      return 0
    fi

    log_info "to_odt_start" "$src" "$out" "LO -> odt" "ext=${ext,,}"
    "$LO_CMD" -env:UserInstallation="$lo_uri" --headless --convert-to odt --outdir "$workdir" "$src" >>"$errfile" 2>&1
    local lo_rc=$?

    produced="$workdir/${base%.*}.odt"
    [ ! -f "$produced" ] && produced="$(ls -1t "$workdir"/*.odt 2>/dev/null | head -n 1 || true)"

    if [ -z "${produced:-}" ] || [ ! -f "$produced" ] || lo_has_error "$errfile"; then
      log_error "to_odt_failed" "$src" "$out" "Не удалось получить .odt" "rc=$lo_rc; tail_err=$(tail -n 160 "$errfile" 2>/dev/null || true)"

      : > "$errfile"
      log_info "to_docx_alt_start" "$src" "$out" "LO -> docx (alt)" ""
      "$LO_CMD" -env:UserInstallation="$lo_uri" --headless --convert-to docx --outdir "$workdir" "$src" >>"$errfile" 2>&1
      produced2="$workdir/${base%.*}.docx"
      [ ! -f "$produced2" ] && produced2="$(ls -1t "$workdir"/*.docx 2>/dev/null | head -n 1 || true)"

      if [ -n "${produced2:-}" ] && [ -f "$produced2" ] && ! lo_has_error "$errfile"; then
        cp -f "$produced2" "$staged_docx" >>"$errfile" 2>&1 || true
        log_info "to_docx_alt_ok" "$src" "$out" "Получен .docx (alt)" "produced=$produced2"
        if run_pandoc_to_fb2 "$staged_docx" "" "$src" "$out" "$errfile"; then
          echo "OK|lo-docx-alt|$src|$out"
        else
          detail="$(short_fail_reason "$errfile")"
          echo "FAIL|pandoc:${detail:0:140}|$src|$out"
        fi
        rm -rf "$tmproot" >/dev/null 2>&1 || true
        return 0
      fi

      if fallback_convert "$src" "$out" "$ext" "$workdir" "$errfile"; then
        echo "OK|fallback|$src|$out"
      else
        detail="$(short_fail_reason "$errfile")"
        echo "FAIL|fallback:${detail:0:140}|$src|$out"
      fi
      rm -rf "$tmproot" >/dev/null 2>&1 || true
      return 0
    fi

    cp -f "$produced" "$staged_odt" >>"$errfile" 2>&1 || {
      log_error "stage_failed" "$src" "$out" "Не удалось подготовить .odt для pandoc" "tail_err=$(tail -n 120 "$errfile" 2>/dev/null || true)"
      echo "FAIL|stage-odt|$src|$out"
      rm -rf "$tmproot" >/dev/null 2>&1 || true
      return 0
    }

    log_info "to_odt_ok" "$src" "$out" "Получен .odt" "produced=$produced rc=$lo_rc"
    if run_pandoc_to_fb2 "$staged_odt" "" "$src" "$out" "$errfile"; then
      echo "OK||$src|$out"
    else
      detail="$(short_fail_reason "$errfile")"
      echo "FAIL|pandoc:${detail:0:140}|$src|$out"
    fi

    rm -rf "$tmproot" >/dev/null 2>&1 || true
    return 0
  fi

  echo "SKIP|unsupported|$src|$out"
  rm -rf "$tmproot" >/dev/null 2>&1 || true
  return 0
}

###############################################################################
# Main loop (single-thread)
###############################################################################
log_info "scan_start" "$DIR" "" "Старт" "log=$LOGFILE dryrun=$DRYRUN overwrite=$OVERWRITE skip_newer=$SKIP_NEWER only=$ONLY excludes_count=${#EXCLUDES[@]} author_set=$AUTHOR_SET author=$AUTHOR title_mode=$TITLE_MODE toc=$TOC toc_depth=$TOC_DEPTH log_level=$LOG_LEVEL"

TOTAL="$(find "$DIR" -type f \( -iname '*.doc' -o -iname '*.docx' -o -iname '*.rtf' -o -iname '*.odt' \) -print | wc -l | awk '{print $1}')"
case "$TOTAL" in ''|*[!0-9]*) TOTAL=0 ;; esac
[ "$TOTAL" -gt 0 ] || TOTAL=1

i=0

while IFS= read -r -d '' f; do
  i=$((i+1))
  pct=$(( i * 100 / TOTAL ))

  line="$(convert_one "$f")"
  status="${line%%|*}"
  rest="${line#*|}"
  detail="${rest%%|*}"
  rest2="${rest#*|}"
  src="${rest2%%|*}"
  out="${rest2#*|}"

  printf '[%6d/%-6d | %3d%%] %-4s %s -> %s' "$i" "$TOTAL" "$pct" "$status" "$src" "$out"
  [ -n "$detail" ] && printf ' (%s)' "$detail"
  printf '\n'
done < <(find "$DIR" -type f \( -iname '*.doc' -o -iname '*.docx' -o -iname '*.rtf' -o -iname '*.odt' \) -print0)

echo "Готово. Лог: $LOGFILE"
