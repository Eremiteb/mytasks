# `utility_bills`: квитанции ЖКУ

Общие правила — [AI.md](../AI.md), проект — [mytasks.md](../mytasks.md). Устройство, настройки, проверки — [utility_bills/README.md](../../utility_bills/README.md).

- Подпроект `utility_bills/`; обёртка `utility_bills.sh`; таймер и юнит — `systemd/user/`; CI `.github/workflows/utility-bills.yml`. Рабочие `utility_bills.json`, БД, venv в Git не входят.
- Тесты — только синтетические данные (репозиторий публичный): реальные квитанции с адресом и лицевым счётом в Git и фикстуры не добавлять.
- Схема БД — только миграцией (`PRAGMA user_version`).
- При выносе `db_path` за каталог проекта дополнять `ReadWritePaths=` в `systemd/user/utility-bills.service`.
- Заполненный `imap.password` в конфиге главнее переменной окружения.
- Перед боевым запуском — `./utility_bills.sh --dry-run` (на реальных PDF — `--dry-run --file …`).
