"""Тесты чистой логики live_subtitles (без GPU, сети и звука). Запуск: venv/bin/python -m pytest tests -q"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import manage_models as mm  # noqa: E402
import subtitles as S  # noqa: E402


def test_collapse_repeats_keeps_normal_text():
    assert S.collapse_repeats("міне, міне, міне, міне. Сәлем") == "міне. Сәлем"
    assert S.collapse_repeats("бір екі бір екі") == "бір екі бір екі"


def test_hallucination_filter():
    cfg = dict(S.DEFAULTS)
    assert S.is_hallucination(cfg, "құрыл " * 40, 3.0)                       # зацикливание
    assert S.is_hallucination(cfg, "а" * 400, 2.0)                           # нереальная плотность текста
    assert not S.is_hallucination(cfg, "Сәлеметсіз бе, бүгін ауа райы жақсы.", 3.0)


def test_selected_json_overrides_defaults_but_not_config(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_DIR", str(tmp_path))
    (tmp_path / "selected.json").write_text(json.dumps(
        {"whisper_model": "/x/model", "llama_model": "m", "_meta": {"a": 1}, "лишний_ключ": 1}), encoding="utf-8")
    assert S.load_selected() == {"whisper_model": "/x/model", "llama_model": "m"}     # служебные и чужие ключи отброшены
    conf = tmp_path / "config.toml"
    conf.write_text('llama_model = "из-конфига"\n', encoding="utf-8")
    args = types.SimpleNamespace(config=str(conf), whisper_model=None, nllb_model=None, translator=None,
                                 llama_model=None, no_original=False, source=None, service=True)
    cfg = S.load_config(args)
    assert cfg["whisper_model"] == "/x/model"          # выбор менеджера применён
    assert cfg["llama_model"] == "из-конфига"          # config.toml важнее выбора менеджера
    assert cfg["service"] is True


def test_selected_json_missing_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "STATE_DIR", str(tmp_path / "нет"))
    assert S.load_selected() == {}


def test_choose_prefers_quality_within_limit_then_speed():
    res = [{"n": "медленная", "q": 48.0, "t": 15.0}, {"n": "быстрая", "q": 46.4, "t": 0.8},
           {"n": "средняя", "q": 46.2, "t": 0.5}, {"n": "слабая", "q": 40.0, "t": 0.1}]
    best = mm.choose(res, key_good=lambda r: r["q"], key_speed=lambda r: r["t"], limit_key="t", limit=2.0, tie=0.5)
    assert best["n"] == "средняя"            # 48,0 отсечена лимитом; 46,4 и 46,2 — ничья, побеждает более быстрая
    assert mm.choose(res, lambda r: r["q"], lambda r: r["t"], "t", 0.01, 0.5) is None


def test_safe_to_convert_rejects_lora_and_pickle():
    sib = lambda *names: types.SimpleNamespace(siblings=[types.SimpleNamespace(rfilename=n) for n in names])  # noqa: E731
    assert mm.safe_to_convert(sib("model.safetensors", "config.json"))
    assert not mm.safe_to_convert(sib("adapter_model.safetensors", "adapter_config.json"))   # LoRA-адаптер
    assert not mm.safe_to_convert(sib("pytorch_model.bin", "config.json"))                   # pickle-веса


def test_fingerprint_changes_with_hardware():
    a = {"gpu": "RTX 3060 Ti", "models_ini": "1"}
    assert mm.fingerprint(a) == mm.fingerprint(dict(a))
    assert mm.fingerprint(a) != mm.fingerprint({**a, "gpu": "RTX 5090"})


def test_models_toml_is_consistent():
    spec = mm.load_spec()
    assert {"asr_max_rtf", "mt_max_p90_sec", "mt_tie_chrf", "update_every_days", "prune_unused"} <= set(spec["selection"])
    assert all(e["kind"] in ("ct2-convert", "faster-whisper") for e in spec["asr"])
    assert all(e["kind"] in ("llama", "nllb") for e in spec["mt"])
    assert S.DEFAULTS["lid_model"] in {e["repo"] for e in spec["asr"]}     # модель для определения языка — среди кэшируемых
