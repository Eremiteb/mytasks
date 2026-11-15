# `utility_bills`: квитанции ЖКУ

Справка проекта mytasks по теме. Общие правила — [AI.md](../AI.md), правила проекта — [mytasks.md](../mytasks.md).

## `utility_bills`: квитанции ЖКУ

- Подпроект `utility_bills/` (исходники, тесты, README, конфиг-пример в Git; рабочий `utility_bills.json`, БД, venv — нет), обёртка `utility_bills.sh`, таймер и юнит в `systemd/user/`, CI `.github/workflows/utility-bills.yml`. Устройство, настройки, проверки и ограничения — `utility_bills/README.md`; не дублировать их здесь.
- Тесты — только синтетические данные (репозиторий публичный): реальные квитанции с адресом и лицевым счётом в Git и фикстуры не добавлять.
- Схема БД меняется только миграцией (`PRAGMA user_version`), не правкой существующей схемы.
- При выносе `db_path` за каталог проекта дополнять `ReadWritePaths=` в `systemd/user/utility-bills.service`.
- Заполненный `imap.password` в конфиге главнее переменной окружения.
- Перед боевым запуском — `./utility_bills.sh --dry-run` (на реальных PDF — `--dry-run --file …`).
