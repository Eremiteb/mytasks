#!/usr/bin/env fish
# Запуск живых субтитров одной командой: ./run.fish [аргументы subtitles.py]
# Первый запуск сам создаёт venv (Python 3.12 через uv) и ставит зависимости.

set here (path dirname (status filename))
set py $here/venv/bin/python

if not test -x $py
    echo "[run] создаю venv и ставлю зависимости (один раз)…"
    uv venv --python 3.12 $here/venv; or exit 1
    uv pip install --python $py torch --index-url https://download.pytorch.org/whl/cu128; or exit 1
    uv pip install --python $py -r $here/requirements.txt; or exit 1
end

# Кэш моделей Hugging Face — рядом с моделями llama.cpp.
set -q HF_HOME; or set -gx HF_HOME /var/lib/llama.cpp/hf-cache

# Запасной путь для ctranslate2: cuDNN/cuBLAS из пакетов nvidia-* в venv.
set nv_libs (path filter -d $here/venv/lib/python3.12/site-packages/nvidia/*/lib)
if test (count $nv_libs) -gt 0
    set -gx LD_LIBRARY_PATH (string join : $nv_libs) $LD_LIBRARY_PATH
end

# Правило KWin (поверх всех окон) — ставится один раз.
if type -q kreadconfig6; and not string match -q '*live-subtitles-overlay*' (kreadconfig6 --file kwinrulesrc --group General --key rules)
    $py $here/subtitles.py --install-kwin-rule
end

# Лог последнего запуска — для разбора, если субтитры пропали.
set -l cache_dir $HOME/.cache/live_subtitles
mkdir -p $cache_dir
$py -u $here/subtitles.py $argv 2>&1 | tee $cache_dir/last.log
exit $pipestatus[1]
