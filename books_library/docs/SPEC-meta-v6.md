# books_library: максимум метаданных книг + сборники (схема v6)

Проект: `/home/esimych/esimych-docs/mytasks/books_library/books_library.py` (stdlib), тесты `tests/test_books_library.py`,
`README.md`, `ai-md/topics/books.md`, DEPS `conf/check_mytasks.requirements`. По-русски. Тесты/линтеры/синтаксис НЕ
запускать (CI), на реальных данных НЕ запускать, не коммитить. Правила — `ai-md/topics/books.md`. Вход недоверенный
(infosec): XML без внешних сущностей (как `_parse_fb2`), внешние программы — список аргументов, timeout, stdin=DEVNULL,
лимиты размера; пути не из данных.

## 1. Миграция v6 (MIGRATIONS, DB_LATEST растёт сам)
- `ALTER TABLE book_collections ADD COLUMN subsection TEXT NOT NULL DEFAULT ''` и `kind TEXT NOT NULL DEFAULT ''`.
  Заполнить subsection для существующих строк из orig_path: часть пути между папкой сборника
  (`_<раздел>/<сборник>/`) и именем файла, например `_Сборники/Библиотека Мошкова/Мошков часть 4/LIB/trnslsf/x.txt` →
  `Мошков часть 4/LIB/trnslsf` (в SQL через substr/instr или отдельным Python-шагом миграции, если migrate_database это
  позволяет — посмотри, как устроены MIGRATIONS; если только SQL — сделай пересчёт в rebuild_collections для строк с
  пустым subsection).
- Новая таблица `book_meta(path TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY(path, key, value))
  WITHOUT ROWID` + индекс `(key, value)`: многозначные метаданные (ключ → значения). relink должен переносить
  book_meta так же, как book_collections (найди relink и UPDATE OR IGNORE ... book_collections — сделай то же для
  book_meta); удаление книги из books (scan removed) — удалить её book_meta.
- VIEW collections не ломать (subsection в нём не нужен; новый VIEW `collection_sections` — section, collection,
  subsection, books).

## 2. Сборник с подразделом
`rebuild_collections` и `path_collection`: при вставке folder- и dedup-строк заполнять subsection (см. выше) и
kind='folder'. Существующие строки не переписывать, только дополнять пустой subsection.

## 3. Сборник из метаданных — отдельно
Строки book_collections с `source='meta'`, section='' , collection=значение, subsection='', kind:
- `edition` — fb2 `publish-info/book-name`, если не совпадает (author_norm) с названием книги;
- `publisher-series` — fb2 `publish-info/sequence@name` (номер — в book_meta `publisher_series_no`);
- `set` — epub `<meta property="belongs-to-collection">` с `collection-type` = `set` (refines), а series — как сейчас.
Заполняет scan (upsert_book), при пересканировании книги её meta-строки заменяются (DELETE … source='meta' + INSERT).

## 4. Максимум полей (в book_meta; основные колонки books — как сейчас)
- fb2 (`fb2_meta`): title-info — все `author` (полностью: first/middle/last/nickname/email/id → `author_full`),
  `translator` (→ `translator`), `keywords`, `date` (+ value), `src-lang`, `lang`, все `sequence` (вторая и далее →
  `series_extra` «имя #номер»), `coverpage`, `annotation` (есть); src-title-info (`src_title`, `src_author`);
  document-info (`doc_author`, `program_used`, `doc_date`, `src_url`, `src_ocr`, `doc_id`, `doc_version`, `history`
  до 2000 символов); publish-info (`book_name`, `publisher`, `city`, `publish_year`, `isbn`, `publisher_series`);
  custom-info (`custom:<info-type>`).
- epub (OPF): все dc:* — `publisher`, `identifier` (+ ISBN отдельно `isbn` по схеме/префиксу urn:isbn), `subject`
  (в genres? нет — `subject`), `description` (annotation уже), `date`, `contributor` с ролью (`translator` для trl,
  `editor` для edt, `illustrator` для ill, иначе `contributor`), `rights`, `source`, `language`, `coverage`, `type`;
  meta: `calibre:series`/`series_index` (есть), `belongs-to-collection` (+ refines collection-type, group-position),
  `dcterms:modified`, `calibre:rating`, `calibre:timestamp`, `calibre:title_sort`, `calibre:author_link_map`
  не нужен. Версия EPUB (`epub_version`).
- pdf: `pdfinfo -enc UTF-8 -isodates` (poppler; timeout 30 с): Title, Author, Subject, Keywords, Creator, Producer,
  CreationDate, ModDate, Pages, Encrypted, Page size → `pdf:<поле>`; Title/Author/Subject пустые колонки books —
  заполнить, если в books пусто (title — только если не похоже на имя файла/«Microsoft Word - …»), pages → `pages`.
- djvu: `djvused -e 'print-meta; n'` (djvulibre): метаданные (title, author, year, …) и число страниц → `pages`.
- docx/odt: `docx_core` уже есть — расширить: title, creator, subject, keywords, description, created, modified,
  lastModifiedBy; odt — `meta.xml` (dc:title, dc:creator, meta:keyword, meta:document-statistic page-count).
- mobi/azw/azw3: заголовок PalmDB + EXTH (stdlib, struct): 100 author, 101 publisher, 103 description, 104 isbn,
  105 subject, 106 publishingdate, 108 contributor, 503 updated title, 524 language. Ограничить чтение первыми 1 МБ.
- txt/html: из начала текста (TEXT_HEAD_CHARS) — уже есть text_author; добавить `text_author` в book_meta, если найден
  (без папки — просто кандидат), html — `<title>` и `<meta name="author|description|keywords">`.
- Для всех: `words` из TextCache, если есть попадание (без извлечения).
Длинные значения обрезать до 4000 символов, управляющие символы убрать, пустые не писать.

## 5. Повторный разбор
`scan --reparse` (опция): перечитать метаданные всех книг (как будто mtime изменился), с прогрессом и ETA каждые
30–60 с (как у других режимов). Внешние программы (pdfinfo, djvused) — параллельно `workers`. Без --reparse scan
разбирает только новые/изменённые, как сейчас.

## 6. Веб и API
Карточка книги: блок «Сведения» — все book_meta (человекочитаемые подписи ключей словарём), сборники с подразделом
(«Библиотека Мошкова / Мошков часть 4 / LIB / trnslsf») и сборники из метаданных с видом. Фильтр по сборнику — как
сейчас; добавить фильтр `subsection`. Найди, где строится карточка (book_collections(conn, …)) и API книги.

## 7. DEPS
В `conf/check_mytasks.requirements` к строке poppler (pdftotext) добавить `pdfinfo`, к djvulibre — назначение
«метаданные scan». Формат строк — как соседние.

## 8. Тесты
fb2 с publish-info/translator/custom-info → book_meta и book_collections meta(edition, publisher-series); epub с
belongs-to-collection set; subsection из orig_path; relink переносит book_meta; pdfinfo/djvused — через подмену
subprocess (monkeypatch) с образцом вывода; mobi EXTH — сгенерировать минимальный PalmDB в тесте.

## Отчёт
Куски diff по разделам, что не сделано и почему.
