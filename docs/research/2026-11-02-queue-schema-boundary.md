# Декларативная схема очереди: следующий модуль

Статус: согласовано пользователем ответом «продолжай» после обсуждения плана.
Дата исследования: 2026-11-02. Исходная версия candidate: ad59363f.

## Граница и назначение

Перенести из memory_queue.py в scripts/llm_wiki/queue_schema.py ровно три
существующих набора определений: _QUEUE_V3_TABLE_SQL, _QUEUE_V3_INDEX_SQL,
_QUEUE_V3_TRIGGER_SQL, включая оба +=, формирующие окончательный tuple триггеров.
Это 22 таблицы, 5 индексов, 35 триггеров, 888 строк / 8534 токена o200k_base.
AST использует только builtin tuple и локальные имена; импорты не нужны.

Модуль владеет SQL-описанием структуры, не выполняет SQL и не открывает БД.
Очередь импортирует эти определения напрямую. Расчёт QUEUE_V3_SCHEMA_SHA256,
QUEUE_COMPILE_SCHEMA_SHA256, application_id/user_version, создание MigrationStatement,
проверки установленной схемы, migration/repair/adoption и SQL-операции остаются
в memory_queue. Это сохраняет владельца операционной политики и исключает
зависимость декларативного модуля от reliable_memory и compile_progress.
Существующие имена в memory_queue сохраняют identity импортированных tuples.
Удаление прежних тел определений обязательно; второй копии SQL не остаётся.

Не менять ни SQL-строки, ни порядок элементов, ни generated trigger names,
ни version/checksum, ни ограничения/протоколы. Новый модуль не означает новую
схему базы. Не добавлять migration, runtime root, database, dependency или daemon.
Применение к живому vault не входит в этот перенос candidate.

## Почему эта граница

Вариант сохранить SQL в очереди оставляет 888 строк декларативного контракта
вместе с исполнением, хотя у него нет зависимости от исполнителя.
Перенос всей миграции сейчас затянет в модуль классы operational contract,
процедуры repair и межтабличную сверку; это отдельная большая ответственность.
Перенос в SQL-файлы добавляет загрузку файлов, упаковку и пути без пользы для
данной задачи. ORM, генератор схемы или Flyway не нужны: существующий SQL
уже является контрактом установленной системы. Выбраны обычные Python tuples,
единственный владелец, прежняя политика применения и независимые проверки.

## Актуальное исследование

Проверены три независимых первичных источника:

- [SQLite schema table](https://www.sqlite.org/schematab.html): sqlite_schema
  содержит объекты и SQL с нормализацией. Поэтому проверять и исходные строки
  побайтно, и реально созданные объекты; совпадение только catalog SQL слабее
  сохранения исходного контракта.
- [Python sqlite3, версия 3.10](https://docs.python.org/3.10/library/sqlite3.html):
  контекст транзакций и API execute/executescript различаются. При переносе
  не менять механизм исполнения или соединения. Минимальный Python проекта
  сохраняется, хотя 3.10 уже не поддерживается upstream; прогон 3.10 обязателен.
- [Redgate Flyway Validate](https://documentation.red-gate.com/fd/validate-277578898.html),
  обновлено 30 октября 2026: сверка применённых миграций проверяет names/types/checksums.
  Здесь это подтверждает необходимость сохранить оба имеющихся digest;
  внедрение Flyway или переход на его CRC32 не предлагается.

Это обоснование локальной границы, не утверждение универсального превосходства.
Фактический baseline: Python 3.12.3, SQLite 3.45.1; зависимости не обновляются.

## Исследование кода и состояние

Встроенный индекс ad59363f подтверждён changes:stale=false. MCP dependencies
не завершился из-за занятых graph workers. Тот же встроенный code_graph в новом
процессе прочитал актуальную generation и нашёл callers _queue_v3_statements:
_build_queue_v3_candidate и _queue_v3_trigger_statements. Граф неполный;
исходники и AST подтверждают полное отсутствие imports в переносимом блоке.

Поиск всех scripts/tests нашёл обращения к SQL-наборам только в memory_queue.
Оба digest участвуют в compile admission и installation/adoption; их потребители
остаются на старых публичных именах. Включены constructors, validation, trigger
recreation, row counts и repair. Проверки source-identity и durable artifact I/O
рассмотрены: первая связана с методом MemoryQueue и ошибками миграции, вторая —
с filesystem durability/permissions/deadlines. Их перенос не входит в предложение.

Живая установка LLM Wiki остаётся на e95f6a4a; работаем только
в /tmp/llm-wiki-compile-progress. Известные проблемы nightly/capture не исправлены
этим рефакторингом. Частный memory index актуален; private knowledge не публикуется.

## Проверки и будущий полный пакет

До переноса: migration,capture links,reliability adoption,compile admission —
58 passed, 3,61 с. Сохранены хеши каждого набора SQL и обоих combined schema digest
в logs/queue-schema-baseline-contract.json; граф — logs/queue-schema-graph.json.
Сами SQL не дублировать в fixture: использовать проверяемые baseline digests
и небольшие сценарии поведения, включая реальные запреты trigger/constraints.

После согласия: сначала private decision и STRUCTURE, затем код.
Собственные tests/modules/queue_schema, guide docs/modules/queue-schema.md и
manifest docs/modules/queue-schema-context.json. Проверить:

- AST и исходные определения полностью совпадают; порядок/число/name/digest прежние.
- Import в свежем interpreter не загружает очередь, storage и другие зависимости.
- Реальное создание всех объектов в isolated SQLite, ограничения и защитные triggers.
- Изменение/удаление одного определения обнаруживается guard; старые aliases identity.
- Старый и новый base/compile checksums совпадают; baseline integration и corruption
  tests проходят, затем общие complexity/structure/CI checks, Ruff и Python 3.10.
- Полный manifest со всеми own tests/doc/instructions/dependencies ≤100000 o200k_base,
  без сокрытия необходимых файлов. Итоговый размер пока не измерен: модуль ещё не создан.

Исходный блок 8534 токена — не размер полного пакета. Если полный пакет превысит
100000, не ослаблять бюджет и не дробить по случайному числу строк.
После результата: measured CI weights, commit/push, актуализация code index,
частной записи прогресса и освобождение только собственных test/candidate caches.
Полный suite, native platform qualification и live rollout не заявлять без запуска.

Дополнительный baseline: tests/test_queue_v3_corruption.py — 29 passed, 12,01 с;
logs/queue-schema-corruption-baseline.txt. Это проверки исходной реализации,
а не доказательство ещё не выполненного переноса. Ответ пользователя
«по лучшим практикам?» уточняет обоснование и не считается согласием.


## Реализация согласованной границы

До кода записаны private decision и STRUCTURE. Блок SQL перенесён побайтно,
оставшийся AST очереди совпал после исключения imports и перенесённых определений.
Источник: logs/queue-schema-preservation.json. Boundary guard перед переносом
падал на старых определениях, после переноса проходит. Владелец не имеет imports.

18 собственных проверок прошли: ordered baseline hashes, намеренное изменение
или удаление определения, создание всех объектов в реальной SQLite, CHECK/FK,
неизменяемая история и отказ удаления без authorization, успешное разрешённое
удаление, свежий interpreter без runtime imports. Дублирования SQL в fixture нет.
Первоначальный полный пакет:76964 o200k_base токена/23 файла.
Интеграционная проверка сравнивает identity aliases и оба combined fingerprints.


## Квалификация результата

Расширенный набор: 293 passed, 65,40 с — own, migration, capture links,
adoption, compile admission, corruption, memory queue, global complexity/shape,
structure и CI policy. Python 3.10 isolated locked dev environment:
106 passed, 18,25 с — own и пять интеграционных файлов baseline.
Оба combined fingerprints совпали с ad59363f. Ruff и git diff --check прошли.
Исходник memory_queue уменьшился с 129165 до 120706 токенов, все 11 пакетов ниже 100000.
Это не полный regression suite, не native Windows/macOS qualification и не
live rollout. Изменения сохраняются только в candidate ветке.

Evidence: logs/queue-schema-own.txt; logs/queue-schema-related.txt/.xml;
logs/queue-schema-python310.txt; logs/queue-schema-all-budgets.json;
logs/queue-schema-final-budget.txt. CI веса новых тестов измерены из JUnit;
финальный бюджет включает этот отчёт. SQL сохранился побайтно и после Ruff.
