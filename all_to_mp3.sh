#!/bin/sh

set -eu

###############################################################################
# SCRIPT ID / PATHS
###############################################################################
SCRIPT_NAME=$(basename -- "$0")
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P)
CONFIG_DIR="${SCRIPT_DIR}/conf"
CONFIG_FILE="${CONFIG_DIR}/audio_to_mp3.conf"
LOG_TEMPLATE_FILE="${CONFIG_DIR}/log_template.conf"
mkdir -p "${CONFIG_DIR}"

if [ -r "${LOG_TEMPLATE_FILE}" ]; then
  # shellcheck source=/dev/null
  . "${LOG_TEMPLATE_FILE}"
fi

###############################################################################
# CONFIG
###############################################################################
if [ -f "${CONFIG_FILE}" ]; then
  # shellcheck source=/dev/null
  . "${CONFIG_FILE}"
  echo "Загружен конфиг: ${CONFIG_FILE}"
else
  echo "Конфиг не найден, используются значения по умолчанию"
fi

: "${FORMATS:=}"
: "${QUALITY:=0}"
: "${COVER_NAMES:=cover.jpg folder.jpg front.jpg}"
: "${JOBS:=0}"
: "${OUTPUT_DIR:=/copy/Music}"
: "${ERROR_LOG:=audio_to_mp3_errors.log}"
: "${REPORT_FILE:=audio_to_mp3_report.txt}"

[ "${JOBS}" -eq 0 ] && JOBS=$(nproc 2>/dev/null || echo 1)

###############################################################################
# ARGS
###############################################################################
ROOT_DIR="${1:-}"
if [ -z "${ROOT_DIR}" ] || [ ! -d "${ROOT_DIR}" ]; then
  echo "Использование: ${SCRIPT_NAME} /путь/к/папке"
  exit 1
fi

###############################################################################
# MAIN
###############################################################################
mkdir -p "${OUTPUT_DIR}"

echo "===== Ошибки | $(date) =====" >> "${OUTPUT_DIR}/${ERROR_LOG}"
echo "===== Отчёт конвертации | $(date) =====" >> "${OUTPUT_DIR}/${REPORT_FILE}"

# shellcheck disable=SC2016
process_file='
FILE="$1"
REL_PATH=$(realpath --relative-to="'"${ROOT_DIR}"'" "${FILE}" 2>/dev/null || echo "${FILE}")
DIRNAME=$(dirname "${REL_PATH}")
BASENAME=$(basename "${FILE}")
OUTPUT_DIR_FULL="'"${OUTPUT_DIR}"'/${DIRNAME}"
mkdir -p "${OUTPUT_DIR_FULL}"
OUTPUT="${OUTPUT_DIR_FULL}/${BASENAME%.*}.mp3"
TMP_COVER=""

echo "Обрабатываю: ${REL_PATH}"

[ -f "${OUTPUT}" ] && exit 0

ffprobe -v error "${FILE}" 2>/dev/null || {
  echo "UNSUPPORTED | ${FILE}" >> "'"${OUTPUT_DIR}/${ERROR_LOG}"'"
  exit 0
}

COVER=""
for name in '"${COVER_NAMES}"'; do
  [ -f "$(dirname "${FILE}")/${name}" ] && COVER="$(dirname "${FILE}")/${name}" && break
done

if [ -z "${COVER}" ]; then
  TMP_COVER=$(mktemp --suffix=.jpg 2>/dev/null || mktemp XXXXXXXXXX.jpg)
  ffmpeg -y -i "${FILE}" -an -vcodec copy "${TMP_COVER}" 2>/dev/null || true
  [ -f "${TMP_COVER}" ] && COVER="${TMP_COVER}"
fi

if [ -n "${COVER}" ]; then
  ffmpeg -y -i "${FILE}" -i "${COVER}" -map 0:a -map 1:v -map_metadata 0 -vn \
  -c:a libmp3lame -q:a '"${QUALITY}"' -c:v mjpeg \
  -metadata:s:v title="Album cover" -metadata:s:v comment="Cover (front)" \
  "${OUTPUT}" 2>> "'"${OUTPUT_DIR}/${ERROR_LOG}"'"
else
  ffmpeg -y -i "${FILE}" -map_metadata 0 -vn -c:a libmp3lame -q:a '"${QUALITY}"' \
  "${OUTPUT}" 2>> "'"${OUTPUT_DIR}/${ERROR_LOG}"'"
fi

if [ $? -eq 0 ]; then
  echo "OK | ${FILE} -> ${OUTPUT}" >> "'"${OUTPUT_DIR}/${REPORT_FILE}"'"
else
  echo "ERROR | ${FILE}" >> "'"${OUTPUT_DIR}/${ERROR_LOG}"'"
fi

[ -n "${TMP_COVER:-}" ] && [ -f "${TMP_COVER}" ] && rm -f "${TMP_COVER}"
'

if [ -n "${FORMATS}" ]; then
  FIND_EXPR=""
  for ext in ${FORMATS}; do
    FIND_EXPR="${FIND_EXPR} -iname '*.${ext}' -o"
  done
  FIND_EXPR=$(echo "${FIND_EXPR}" | sed 's/ -o$//')
  # shellcheck disable=SC2086
  find "${ROOT_DIR}" -type f \( ${FIND_EXPR} \) -print0 | xargs -0 -n 1 -P "${JOBS}" sh -c "${process_file}" _
else
  find "${ROOT_DIR}" -type f -print0 | xargs -0 -n 1 -P "${JOBS}" sh -c "${process_file}" _
fi

TOTAL_SRC=$(find "${ROOT_DIR}" -type f -print0 | xargs -0 -n 1 ffprobe -v error 2>/dev/null | wc -l)
TOTAL_OK=$(grep -c "^OK |" "${OUTPUT_DIR}/${REPORT_FILE}" 2>/dev/null || true)

{
  echo
  echo "===== СВОДКА ====="
  echo "Исходных файлов найдено : ${TOTAL_SRC}"
  echo "Успешно сконвертировано : ${TOTAL_OK}"
  echo "Лог ошибок              : ${OUTPUT_DIR}/${ERROR_LOG}"
} >> "${OUTPUT_DIR}/${REPORT_FILE}"

echo "Готово."
echo "Отчёт: ${OUTPUT_DIR}/${REPORT_FILE}"
echo "Лог ошибок: ${OUTPUT_DIR}/${ERROR_LOG}"
