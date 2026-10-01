#!/usr/bin/env python3
"""Конвертирует дообученную на казахском модель Whisper в формат CTranslate2 для faster-whisper.

Запуск (из каталога live_subtitles):  venv/bin/python convert_whisper.py [repo_id]
По умолчанию — abilmansplus/whisper-turbo-ksc2 (WER 10,4% на FLEURS kk против 22,9% у large-v3-turbo).
Результат: /var/lib/llama.cpp/whisper-ct2/<имя модели> (путь прописан в whisper_model).
"""
import os
import shutil
import subprocess
import sys

os.environ.setdefault("HF_HOME", "/var/lib/llama.cpp/hf-cache")
from huggingface_hub import hf_hub_download  # noqa: E402

repo = sys.argv[1] if len(sys.argv) > 1 else "abilmansplus/whisper-turbo-ksc2"
out = os.path.join("/var/lib/llama.cpp/whisper-ct2", repo.split("/")[-1])
here = os.path.dirname(os.path.abspath(__file__))
subprocess.run([os.path.join(here, "venv/bin/ct2-transformers-converter"), "--model", repo, "--output_dir", out,
                "--quantization", "float16", "--force"], check=True)
# У turbo 128 мел-каналов: без preprocessor_config.json faster-whisper возьмёт 80 и распознавание сломается.
for name in ("preprocessor_config.json", "tokenizer.json"):
    if not os.path.exists(os.path.join(out, name)):
        shutil.copy(hf_hub_download("openai/whisper-large-v3-turbo", name), os.path.join(out, name))
print("готово:", out, sorted(os.listdir(out)))
