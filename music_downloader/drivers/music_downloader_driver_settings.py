"""Применение настроек драйверов из блока `drivers` файла `music_downloader.json`.

Каждый драйвер хранит значения по умолчанию в словаре `SETTINGS` на уровне модуля
и читает их в момент вызова. `apply_driver_settings` накладывает на них значения
из конфига: неизвестные ключи, значения не того типа и отрицательные числа
отбрасываются с предупреждением, остаётся значение по умолчанию.

Файл не является драйвером сайта или источника поиска (в `sites` и
`search_sources` его не указывают); загружается по пути через
`spec_from_file_location`, так как каталог загрузчика не входит в `sys.path`.
"""
import os


def _check(default, value, path):
    """Возвращает `(значение, ошибка)`: проверенное значение того же типа, что и `default`."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value, None
        return None, f"{path}: ожидалось true/false"
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, f"{path}: ожидалось число"
        if value < 0:
            return None, f"{path}: число не может быть отрицательным ({value})"
        return value, None
    if isinstance(default, str):
        if isinstance(value, str):
            return value, None
        return None, f"{path}: ожидалась строка"
    if isinstance(default, (list, tuple)):
        if not isinstance(value, (list, tuple)):
            return None, f"{path}: ожидался список"
        # Диапазон [min, max] (`*_range_seconds`) — ровно та же длина; прочие списки
        # (расписания повторов и т.п.) — любой длины, элементы того же типа.
        if path.endswith("_range_seconds") or not default:
            if len(value) != len(default):
                return None, f"{path}: ожидалось элементов {len(default)}, получено {len(value)}"
            item_defaults = list(default)
        else:
            item_defaults = [default[0]] * len(value)
        checked = []
        for index, (item_default, item) in enumerate(zip(item_defaults, value, strict=True)):
            checked_item, error = _check(item_default, item, f"{path}[{index}]")
            if error:
                return None, error
            checked.append(checked_item)
        if path.endswith("_range_seconds") and len(checked) == 2 and checked[0] > checked[1]:
            return None, f"{path}: минимум больше максимума ({checked[0]} > {checked[1]})"
        return type(default)(checked), None
    if isinstance(default, dict):
        if not isinstance(value, dict):
            return None, f"{path}: ожидался объект"
        merged = dict(default)
        for key, item in value.items():
            if key not in default:
                return None, f"{path}: неизвестный ключ {key!r}"
            checked_item, error = _check(default[key], item, f"{path}.{key}")
            if error:
                return None, error
            merged[key] = checked_item
        return merged, None
    return None, f"{path}: тип значения по умолчанию {type(default).__name__} не поддерживается"


def apply_driver_settings(module, settings, log):
    """Накладывает `settings` (словарь из конфига) на `module.SETTINGS`; ошибки — в `log.warning`."""
    defaults = getattr(module, "SETTINGS", None)
    if not isinstance(defaults, dict) or not settings:
        return
    name = os.path.splitext(os.path.basename(getattr(module, "__file__", None) or ""))[0] or getattr(module, "__name__", "?")
    if not isinstance(settings, dict):
        log.warning(f"Настройки драйвера {name}: ожидался объект, настройки игнорируются")
        return
    for key, value in settings.items():
        if key not in defaults:
            log.warning(f"Настройки драйвера {name}: неизвестный ключ {key!r} игнорируется")
            continue
        checked, error = _check(defaults[key], value, key)
        if error:
            log.warning(f"Настройки драйвера {name}: {error}; остаётся значение по умолчанию")
            continue
        defaults[key] = checked
