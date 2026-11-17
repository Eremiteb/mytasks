# `music_downloader`: конфигурация и поведение

Общие правила — [AI.md](../AI.md), проект — [mytasks.md](../mytasks.md). Устройство, ключи, режимы, коды — [music_downloader/README.md](../../music_downloader/README.md).

## Конфиг и каталоги

- `download_paths` — обязательный непустой список в корне JSON, общий для всех мостов (трек — во все каталоги); внутрисайтовых нет. Синхронно править рабочий JSON, `.json.example` и README; реальные пути и лимиты примерами не заменять.
- `max_size_mb` (1024 МиБ) — весь каталог с подкаталогами; проверка при старте и перед каждым сохранением; `save_response` проверяет каждый блок потока (и без `Content-Length`), учитывает копии в дочерних каталогах.
- `DownloadLimitExceeded`: удалить `.part`, код `2`, трек в БД не писать, сортировщик не запускать; скачанное не удалять. Контроль на один процесс; для сторонних записей — квоты ФС.
- Код `2`: нет/пустой/неверного типа глобальный список, нет или уже заполнены каталоги.
- `download_max_seconds` (600) и `download_max_mb` (100): превышение — ошибка одного трека, запуск продолжается.
- Безопасность: адреса только `http`/`https`; драйверы — только `.py` в `drivers/` после `realpath`; `.part` с `O_NOFOLLOW`; префикс `_` для имён Windows.

## Очередь и метрики

- Round-robin по мостам (`fill_queue`/`read_category` в `run`): по треку от моста за круг. Очередь моста пополняется его следующей категорией только когда опустела (ссылки протухают); остальные мосты не ждут. Тест — `test_run_round_robin_refills_each_site_without_waiting_for_big_queue`.
- Метрики — `report_run` в `finally` (и при коде `2`): `Итог <мост>`, `Итог поиска <источник>`, `Место: <папка>` (WARNING при < 50 треков), `Итог запуска`, `bari: …` (совместимость); таблица `runs` (JSON `sites`, `sources`, `destinations`). Драйверы пишут `track["_search_stats"]` (`queries/candidates/exact/relaxed/chosen/errors/seconds`; нет ключа — ноль). Новая метрика — в `site_data["stats"]`/`report_run` и тест `test_run_writes_metrics_to_log_and_runs_table`.

## TLS

- Не ослаблять. HTTPS — штатный `HTTPAdapter()` (`scraper.mount("https://", HTTPAdapter())` в `run`): сертификат и имя хоста, SECLEVEL по умолчанию.
- `SSLAdapter` с `DEFAULT@SECLEVEL=1` снят 2026-11-12: все 25 хостов драйверов проходят на SECLEVEL=2.
- Не ставить HTTPS на `CipherSuiteAdapter` cloudscraper: только `prime256v1`, `cdn2/cdn3.sefon.pro` падают с `SSLV3_ALERT_HANDSHAKE_FAILURE`.
- Новый хост с `dh key too small`/`ca md too weak` — ослаблять только для него (`scraper.mount("https://<хост>/", …)`) с причиной здесь. Тест — `test_run_uses_global_destinations_for_all_sites`.

## База и дедупликация (`downloads`)

- `artist/title` — служебные нормализованные ключи; `original_artist/original_title` — исходные строки драйвера (для отчётов).
- `normalize_db_text` = NFKC + `casefold` + только `str.isalnum()`: «Вася & Петя» == «Вася +Петя» == «ВасяПетя»; слова не удалять («Song (Remix)» != «Song»).
- Ключи: ID `<site>_<track-id>` и `artist/title` (`UNIQUE`). По содержимому: `save_response` → `(bytes, paths, sha256_hex)`; совпадение `filehash` — копии удаляются, строка с `filehash = NULL` (`INSERT OR IGNORE`); хеш после загрузки.
- Индексы: `UNIQUE(artist,title)` и **частичный** `UNIQUE(filehash) WHERE filehash IS NOT NULL`.
- При открытии — перенормализация, схлопывание дублей к самой свежей; регистр и пунктуация, потерянные прежней версией, не восстанавливаются.
- Схема — только миграцией: `PRAGMA user_version`; новая миграция — следующий номер в `DB_MIGRATIONS` (`DB_LATEST`); DDL — только в `DB_SCHEMA`/`table_ddl`; изменение = правка `DB_SCHEMA` + миграция через `_rebuild_table`; **не** `ALTER TABLE ADD COLUMN`, руками не править.
- `migrate_database` — в начале `_create_table`, ДО перенормализации и дедупа. Новая база сразу `DB_LATEST`; существующая — бэкап `conn.backup()` в `<база>.bak-v<версия>-<ГГГГММДД-ЧЧММСС>` и миграция в транзакции. Миграция 1 пересобирает `downloads`/`runs`; индексы воссоздаются только те, что были.
- Порядок в `_create_table`: оба индекса снять ДО `UPDATE` и дедупа, создать ПОСЛЕ (иначе `UPDATE` нарушит живой UNIQUE).
- `normalize_db_text` менять только с бэкапом `music_downloader.db` (миграция удаляет схлопнувшиеся строки).
- `music_downloader.py --migrate-db [--dry-run]` — только миграции; dry-run печатает `[dry-run] будет: …`.
- Backfill `filehash` выполнен 2026-09-06 (скрипт удалён); `NULL` (файл не найден) при штатной работе не досчитывается.

## Очистка ID3 (`clean_mp3_tags` в `save_response`, нужен `mutagen`)

- В тегах — только данные о песне. Из полей песни вырезать ссылки (со скобками или подводкой-глаголом «скачано с…»; одиночные «by»/«on» не трогать); строки текста песни со ссылкой удалять, `\r\n` сохранять (иначе неидемпотентно).
- `TIT2`/`TPE1` целиком не удалять. TLD `by` и похожие на слова зоны не распознавать («Prod.by»); почта — не ссылка.
- В комментарий: `TCOP`/`TPUB`/`TXXX` со ссылкой, `W***`, `TOWN`/`TRSN`/`TRSO`, TXXX `comment`/`description`/`notes`. Удалить: `TENC`, `TSSE`, `TLEN`, `PRIV`, `GEOB`, iTunes-`COMM`, технические TXXX ffmpeg/iTunes/рипперов (ссылки из них — в комментарий).
- **TXXX целиком не переносить**: в библиотеке это MusicBrainz/ReplayGain/ISRC/LABEL (замер 2026-10-21, ~12 на файл).
- Комментарий один, `optimize_comment` (короткие ссылки без схемы/`www`, без повторов, без hex iTunes и повторов названия/исполнителя/альбома).
- Очистка на копии `.part.tags`; сбой mutagen — предупреждение, файл как есть; размер и `filehash` — от итогового файла. Лежащие файлы не трогать.
- Для лежащих — `music_downloader/music_tags_clean.py` (тот же `clean_mp3_tags`): без `--apply` — dry-run на копиях; `--apply` — бэкап тегов в `state/music-tags-backup-<дата>/`; `--restore <каталог>` — откат.

## Jamendo

- `/` — одна страница до 200 последних (`tracks`, `releasedate_desc`); `/playlist/<ID>` — API плейлистов. Пустые ответы CDN и сбои API — повтор по `api_retry_delays_seconds` (`1, 3, 5` с; `drivers` → `music_downloader_jamendo_com`).
- Только `audiodownload_allowed: true` и непустой `audiodownload`; потоковый `audio` не подставлять. Ключ: `JAMENDO_CLIENT_ID`, затем `client_id` записи `jamendo_com` в `sites`; плейсхолдер `YOUR_JAMENDO_CLIENT_ID` не отбраковывать.
- Только вокал (`vocal_only`, по умолчанию `true`): `include=musicinfo`, `vocalinstrumental=vocal`; `instrumental`/пусто — пропуск; нет `musicinfo` — трек остаётся. Тест — `test_instrumental_tracks_skipped`. Без фильтра ~2/3 новинок — инструментал (замер 2026-11-08).
- Ошибки API и пустые результаты — по-русски, `site=jamendo_com`; проверки метаданных аудио не качают.

## Драйверы, Shazam, MyChords, Beu FM

- Драйвер: `get_tracks(session, base_url, category)`. `resolve_track(session, track)` — только перед скачиванием нового трека, после проверки БД и очереди; `download_url/referer/src` или `None`. `None`/исключение — пропуск без записи в БД, пара удаляется из `queued_tracks`.
- Загрузка через `spec_from_file_location`; соседние драйверы — по пути (`_load_driver`), не `from drivers import`.
- `SETTINGS` — на уровне модуля, код читает `SETTINGS[...]` при вызове. Наложение `drivers.<файл без .py>` — `apply_driver_settings` (`drivers/music_downloader_driver_settings.py`; неизвестный ключ/тип/отрицательное число — warning и умолчание); источникам — `track["_driver_settings"]`. Новый параметр — в `SETTINGS` и `.json.example` (тест сверяет). Регулярки, селекторы, URL-шаблоны в `SETTINGS` не выносить.
- **`shazam_com`:** чарт `/ru-ru/charts/top-200/world` — с Firefox-заголовками `SETTINGS["chart_headers"]` (Chrome-профиль и `curl` без них давали 405/204/чужую страницу).
  - Fastly отдаёт по адресу чарта HTTP 200 со страницей чужой песни (~1.8 МБ вместо ~3.7 МБ) до TTL узла; HTML и RSC ломаются независимо.
  - Каждая попытка — два источника: `parse_chart` (HTML, `[data-test-id="songItem"]`, `$RS(...)`), затем `parse_flight_chart` (RSC `RSC: 1`, `self.__next_f`).
  - Попытки — `SETTINGS["retry_delays_seconds"]` (`15, 45, 90` с), `Connection: close`.
  - **Query-«антикэш» не добавлять** (ключ Fastly чаще указывает на битую копию). Легаси `cdn.shazam.com/shazam/v3/.../genre-global-chart-1` **не использовать**: заморожен на начале 2024.
  - `ERROR` — только если оба источника промолчали на всех попытках. Поломку разбора выдают `В карточке чарта…` / `Ожидался полный Top 200…` / `RSC-поток Shazam…` при живом чарте.
  - RSC без charset → `response.encoding = "utf-8"` в `get_tracks`; не убирать.
  - Аудио — только через `search_sources` (точное `match_key`); превью Shazam/Apple не использовать.
- **Общие источники MP3 — один `search_sources`** в корне `music_downloader.json` (`name`, `enabled`, `driver`, `base_url`, `search_path` с `{query}`); движок кладёт в `track["_search_sources"]`; без ключа — `DEFAULT_SOURCES` (Sefon → LMusic).
  - `find_audio(session, track, label, log)` в `drivers/music_downloader_audio_search.py`: запросы `search_queries` (полный + «первый исполнитель + `clean_title`»); сначала точное `match_key` во всех источниках по порядку, затем `relaxed_match` (`fold_key`: без диакритики и письменности — кириллица ↔ латиница, q/k, kh/h, j/zh; без `(feat. …)` и remaster; общий исполнитель из `artist_keys`).
  - remix/live — разные записи, не ослаблять. `(with X)` — другая запись, но гость X может быть исполнителем кандидата или в `(feat. X)` (`_with_guests`/`_people`).
  - `stream=True` для проверки ссылки; пауза `query_pause_seconds` (1 с). Каждое обращение к источнику — INFO «запросов N, кандидатов M, точных K, мягких L — итог», даже при пустой выдаче.
  - `{query}` — параметр, `{query_path}` — сегмент пути. Драйвер обязан иметь `search_tracks(session, base_url, search_path)`.
  - Порядок — по качеству, не охвату: `sefon.pro`, `lmusic_kz`, `muzofond` (404 = пусто), `drivemusic`, `hitmotop` (128 кбит/с, лучший охват), последним `youtube_music` (`yt-dlp` + `ffmpeg`, VBR ~190; медленный, 20–80 с).
  - Beu FM: `search_translit` → запрос в другой письменности (`to_cyrillic`/`to_latin`).
  - `artist_aliases` (`track["_artist_aliases"]`; только мягкий проход и доп. запросы). Группу добавлять только после проверки, что это тот же исполнитель (Madi Syzdykov = Мәди Сыздықов = Madi Syzdyq = Madistation).
  - Кандидат может нести `fetch()` вместо ссылки: `find_audio` его не проверяет; движок вызывает `fetch()` вместо `scraper.get`.
  - `yt-dlp` и `ffmpeg` — необязательные `DEPS` в `check_mytasks.sh`.
  - Замер 2026-10-24, 132 трека: MP3-сайты 108 (82 %), с `youtube_music`, транслитерацией, синонимами и `(with X)` — 130 (98 %). Не найдены: казахская «Сара Амангелді — Көздерің сенің…» (только оригинал — другая запись), A. R. Rahman — Yakkai Thiri.
  - Источники — не в код мостов. Новый сайт = `search_tracks` + запись в конфиге и `.json.example`. Внешние сайты-кандидаты без согласия пользователя не опрашивать. `mp3party.net` отклонён (CDN отдаёт `failed`).
- **`mychords_net`:** `/ru/novinki` → `li.b-listing__full__item a.b-listing__full__item__name`; разделитель «Исполнитель - Название» — первый `-`, `–` или `—` в пробелах (`_ARTIST_TITLE_SEP_RE`; сайт пишет « – »). ID из `/<id>-slug.html` → `div.b-words__player[data-src]` → iframe `audio.xpleer.com/embed/` → `#file[data-mp3]` на `storage.xpleer.com`; без плеера — `find_audio`.
- **`beufm_kz`:** `base_url` строго `https://beufm.kz` (сертификат на apex; `www` по HTTPS не проходит TLS). Трек — `<span id="track_org">`, пустой в исходном HTML (заполняет `template/Player.js` из `1.txt` раз в 10 с, кириллица — числовые сущности) → при пустом спане читать `1.txt`. Разделитель — первый « - »; строка без разделителя — не трек. ID `fm_<sha256[:16]>`. Аудио — `resolve_track` через `search_sources`. Записи эфира (`_recorded_tracks`); если песни нет ни в одном источнике — запись через `fetch()` (`audio_search.FileResponse`).

## Запись эфира Beu FM (`beufm_recorder.py`)

- Служба `systemd/user/beufm-recorder.service`, `enable --now` (`unit_enable_args` в `check_mytasks.sh`). Настройки — README, «Запись эфира Beu FM».
- Кольцевой буфер `stream.beufm.kz/beufm` (MP3 192 кбит/с) без перекодирования, нарезка по целым кадрам (`whole_frames`; иначе ffmpeg бракует дубль). `1.txt` — раз в 2 с.
- **`1.txt` переписывается раз в минуту (в :01)**: отставание смены названия 0–60 с; по нему резать нельзя.
- Дубль каждого выхода — запас 65 с до и 10 с после. По 3 дублям (`takes`) — `fpcalc` (`choose_take`: общий участок, без `decode_errors`, минимум различий), отступы 0.3/1.5 с. Не сошлись — удалить старый дубль, ждать выход. 2 дубля — только после `max_wait_days`; 1 — не используется.
- Песни из `music_downloader.db` не писать (`in_library` — `normalize_db_text`).
- `aired.jsonl`: `log_aired`, `list_aired`, `prune_aired`, идемпотентный `seed_aired_from_takes` (ID+время). Не «заполнять, только если журнала нет»: служба пишет текущую песню раньше обслуживания.
- Драйвер отдаёт все песни за `aired_lookback_days`, не одну текущую (иначе песни из источников не качались, а дубли копились: 2026-10-27 — 384 дубля, 2 ГБ, 0 готовых).
- `maintenance` раз в `maintenance_minutes`: заполнение журнала, `purge_in_library`, `finalize_overdue`, `prune_aired`, `cleanup`. Записи — `state/beufm_recordings` (не в gitignore).
- Рекордер и драйвер подключают друг друга по пути, драйвер — лениво (иначе цикл). Лог — суточный (`DailyJSONLHandler`).
- Тесты — `music_downloader/tests/test_beufm_recorder.py` (синтетика, без сети); `beufm_recorder.py --dry-run`; служба — задание `health` (`beufm-recorder`, 20 мин).

## Обёртка, логи, тесты

- `music_downloader.sh`: venv из `music_downloader/venv` (если есть), `music_downloader.py`, деактивация venv, `split_by_dash.sh` только при коде `0`; путь сортировщика — отдельная настройка.
- Ошибки драйверов и треков логируются, код может быть `0`; отсутствие JSON тоже даёт `0` без загрузки.
- Логи: `logs/music_downloader_engine-<timestamp>.jsonl` (движок), `logs/music_downloader-<timestamp>.jsonl` (обёртка).
- `tests/test_music_downloader.py` грузит `music_downloader/music_downloader.py` через `spec_from_file_location` (иначе Zed принимает каталог за namespace-пакет). Перед Python-тестами — requirements.
- Документация: корневой README, эта тема, `music_downloader/README.md`; заметки QNAP — не changelog.
