# Завершение декомпозиции LLM Wiki

Дата: 2026-11-02. Статус: согласовано пользователем; самостоятельное уточнение внутренних границ разрешено.
База: bccb6a9c248f0abe5ded75144887d2f01ffc1a81, отдельная рабочая ветка.

## Цель и проверяемое завершение

Один продукт и установка. Каждый модуль имеет одну ответственность, явный
контракт и единственного владельца состояния. Полный пакет сопровождения
(исходники, собственные тесты, данные, документация, обязательные инструкции,
необходимые контракты зависимостей) не превышает 100000 o200k_base токенов.
Межмодульные сценарии и исторические материалы также учитываются; перенос тестов
в «общую» папку не освобождает их от бюджета. Готовность отдельных пакетов не
означает завершение проекта.

Для завершения необходимы:
- Полный реестр владения, проверяемый против git ls-files: нет потерянных файлов.
- Измерение каждого пакета, включая инструкции и разделители при сериализации.
- Однонаправленные проверяемые импорты; каждый перенос проверен по потребителям,
  динамическим вызовам, CLI, subprocess и ресурсам, а не только по AST.
- Сохранены поведение, форматы, атомарность, права доступа и публичные команды.
- Нет дублированных реализаций. Для необходимых совместимых alias указаны
  потребители и проверяемое условие удаления.
- Связанные тесты и полный регрессионный прогон проходят; платформенные пропуски
  явно перечислены и не выдаются за подтверждённую Windows/macOS квалификацию.
- Проверены CCN <=5 и ограничения формы; индекс актуален.
- Токеновая эффективность задачи оценивается отдельно от размера исходников.
  Без парных измерений не заявляется экономия вызовов модели.

## Свежая инвентаризация

2842 отслеживаемых файла, 8090066 токенов. scripts: 1929206; tests: 2391344;
docs: 1874357; benchmark: 1430515. Это сумма текста файлов, без сообщений чата.
14 существующих манифестов включают объединённо 154 файла; это покрытие контекста,
а не доказанное уникальное владение. 2688 файлов ещё вне этих манифестов.

Особые крупные артефакты: uv.lock 334209, benchmark/contradiction-v1.json 209197,
docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md 144744 токена. Их нельзя скрыть как
«не код». Lockfile остаётся единым входом установщика; для сопровождения нужны
проверяемые представления записей с полным покрытием и обратной сборкой.
Если пользователь требует, чтобы каждый физический файл также был <=100000,
единый lockfile требует отдельного решения: это более сильное условие, чем
лимит модуля. Нельзя объявлять его выполненным за счёт выборочного чтения.

Доказательства: logs/decomposition-completion-inventory.json и
logs/decomposition-completion-graph.json в установленном vault. Встроенный MCP
обзор сообщил занятых работников; отдельный процесс code_graph.get_architecture
прочитал индекс. Граф неполон и дополняется исходниками. Индекс исходников
соответствует bccb6a9c; docs и установщики проверяются отдельно.

## Предлагаемый общий архитектурный мандат

Согласовать следующие семейства ответственностей и последовательный перенос
в scripts/llm_wiki/<responsibility>.py либо пакет того же имени, когда несколько
внутренних файлов действительно нужны. Существующий самостоятельный файл можно
квалифицировать на месте без перемещения. Тесты — tests/modules/<responsibility>/,
описание и манифест — docs/modules/. Старые публичные CLI остаются входами.
Не вводить обязательную многоэтажную структуру area/module/api.

Порядок определяется зависимостями, а не количеством файлов:

1. Основа: конфигурация, безопасное файловое IO, SQLite-контракты, admission и
   владение. Убрать зависимости основы от исполнителей без второго реестра.
2. Очередь: проверки схемы; идентичность источников; планирование миграции;
   хранение и переходы; результаты; управление оператором; исполнитель.
   SQL-транзакция и lease settlement сохраняют единого владельца.
3. Markdown-транзакции: образы файлов и манифесты; prepare/CAS/publication;
   восстановление, undo и retention. Не создавать конкурирующих координаторов.
4. Capture и breadcrumbs: ingress, неизменяемое evidence, adoption, terminal
   records. Очередь не подменяет доказательство сохранённого Markdown.
5. Компиляция: источники, планирование, валидация, публикация, журнал прогресса,
   восстановление вызовов. Существующий незавершённый compile-progress не
   объявлять завершённым одной перестановкой кода.
6. Знания: страницы, claims, архивы, project journal и blackboard, learning.
7. Модели: выбор и вызов провайдера, DLP policy/gateway. Один fail-closed барьер.
8. Поколения и поиск: corpus, catalog, graph, vectors, retrieval, evidence
   resolution, answers, context, telemetry. Единственный active pointer.
9. Код: parsing, graph/impact, repository index/revision/retention, navigation.
10. LSP: protocol/security, запуск/containment/cleanup, документы, lifecycle,
    profiles. Сохраняются deadlines, process ownership и платформенные правила.
11. Интерфейсы: MCP tools/transport/supervisor и lifecycle adapters.
12. Установка и обслуживание: install transactions/configuration/release,
    language/model installation, health readers, maintenance, backup/repair.
    Nightly обращается к maintenance, а не заимствует реализацию из doctor.
13. Отдельные сценарные пакеты: capture→terminal, compile→publication/recovery,
    queue→ownership, transaction→recovery, backup→restore, install→upgrade,
    retrieval→answer, code→navigation, LSP containment, nightly, MCP lifecycle.
14. Измерения и история: тематические benchmark и документы, fixtures и отчёты.
    Большие данные разбиваются по существующим записям с проверкой сохранности;
    архивные утверждения не переписываются под новый код.

Это не фиксированное количество будущих модулей. Для каждой ответственности
перед кодом фиксируются точные символы, потребители, необходимый контекст,
baseline и тест нарушения границы. Если пакет превышает бюджет, делится по
самостоятельной обязанности, а не по строкам или размеру произвольного среза.
Предлагается разрешить такие уточнения внутри перечисленных семейств без
повторной подписи на каждый файл. Отклонение от данных рамок требует согласования.
Разрешение получено 2026-11-02: «Да, согласовать весь план и самостоятельное уточнение границ».

## Что не входит в мандат

Нет установки в живой vault, изменения runtime-root, БД, форматов, публичного
поведения, модели безопасности, поддержки Python, зависимостей или релиза.
Нельзя удалять private knowledge или run/, ослаблять тесты и лимиты, вводить
daemon, сервисы, второй продукт или универсальный DI/event framework.
Обнаруженный дефект сначала воспроизводится и исправляется отдельным шагом.

## Исследование и альтернативы

Повторно проверены 2026-11-02 три независимых первичных источника:
- https://martinfowler.com/bliki/BoundedContext.html — границы по модели и
  ответственности; не обосновывает заранее заданное число файлов.
- https://import-linter.readthedocs.io/en/stable/contract_types/ — проверяемые
  forbidden/protected/layers/independence/acyclic contracts. Инструмент не
  устанавливается этим планом; imports не доказывают runtime-изоляцию.
- https://peps.python.org/pep-0544/ — узкие структурные Protocol, когда нужны
  альтернативные исполнители. Не заменяют runtime-валидацию и не нужны везде.

Выбор: последовательная декомпозиция внутри одной поставки. Альтернативы:
только выбор контекста оставляет обратные зависимости; механическое дробление
оставляет связанность; отдельные distributions/сервисы добавляют версии и отказы
без подтверждённой потребности. Сжатие данных допустимо только с доказательством
сохранности; минификация и удаление необходимого контекста отвергнуты.
Сохраняется контракт Python >=3.10; новые технологии и версии не добавляются.

## Уточнение проверки полноты пакета

После SQLite-контрактов обнаружено: все 16 манифестов пропускали автоматически
загружаемые tests/conftest.py (3024 токена) и tests/code_kernel_helpers.py
(4487). Это общий пропуск измерения, не уменьшение кода. Воспроизведение:
logs/module-pytest-bootstrap-underestimate.json. Даже с ними все 16 предварительно
укладываются в лимит; прежние числа занижены. Требуется проверка обязательного
bootstrap из фактического pytest_plugins и родителей тестов, а не ручной список.

Единый test-only измеритель tests/module_context.py заменит повторённый код
16 счётчиков: разворачивает объявленные файлы, добавляет проверяемый обязательный
bootstrap и собственный код в требования к манифесту, считает реальное
сериализованное содержимое с именами и границами файлов. Нельзя молча добавлять
отсутствующее и выдавать старый манифест полным: отсутствие — явный отказ.
Лимит, encoding и файлы не урезаются. Общий conftest и его guards остаются
включёнными; изоляция product imports отдельно проверяется в subprocess.
Pytest-plugin импортирует интеграционные библиотеки, но собственные fixtures
этого плагина не вызываются чистыми module tests; их полные транзитивные исходники
не становятся собственностью каждого модуля. Граф контрактов/вызовов по-прежнему
проверяется отдельно. Этот измеритель не доказывает, что все связанные тесты
всего репозитория уже распределены по владельцам.

Первичные источники проверены 2026-11-02: pytest writing_plugins (автозагрузка
conftest и pytest_plugins), OpenAI tiktoken README (реальное кодирование токенов),
Python pathlib (явное перечисление файлов). Новых зависимостей не вводится.

## Status update 2026-11-09: the decomposition is not complete

The owner decided on 2026-11-09 that the remainder is done now. Measured after that work:
4376 tracked files, 3134 owned by a module packet, 1242 unassigned, about 3.95 million
o200k_base tokens unassigned. Every packet measures at most 100000 tokens (the largest
97343), and no single file is over the limit any more: `uv.lock`, the benchmark data and
the large studies are split at existing records with a byte-exact catalog, and
`scripts/memory_queue.py` is 98298 tokens after four cohesive helpers moved out and its
dead aliases were deleted.

What remains is code: 133 scripts, 1089 tests and 19 benchmark runners. Schemas, queries
and test data now have owner packets, except `tests/fixtures/code_kernel/python/pkg/broken.py`:
it is invalid Python on purpose and the packet validator parses every `.py` it is given.
Only 5 of 134 scripts and 83 of 1170 tests fit their full-source import closure in a
packet beside the roughly 60000-token base; orchestrators reach 1.75 million tokens
across 232 files, including one 77-file import cycle. `memory_queue.py` itself owns no
packet, because the file alone leaves no room for its tests and dependencies.

Those files need interface-only dependencies. The owner accepted that architecture on
2026-11-04, but `_manifest_bundle` refuses a `qualified_dependencies` entry until a
boundary passes fresh qualification, and the rollout proposal authorizes no new paired
agents. Lifting that refusal without the evidence would weaken the check, so it was left
in place. Law 11 stays unconfirmed for the unassigned files.
The owner was asked on 2026-11-09 whether the remainder may move to a separate task, with
the measured cost stated, and answered: no, do it here. The branch is not handed over until
the ownership check passes.

Measured on 2026-11-09 after the structure reference was split: 1243 files are unassigned.
A packet has about 52000 tokens left beside its base. 15 scripts stay over that even with
every dependency reduced to an interface (`doctor.py`, `mcp_server.py`, `compile_memory.py`
and `memory_queue.py` first). 335 test files, 1.09 million tokens, read or patch private
names of providers whose full source does not fit beside them. 106 providers are reached
privately and 127 are imported by tests.

This plan is complete when the ownership registry covers `git ls-files` without a lost
file and every packet measures at most 100000 tokens.

## Status update 2026-11-11: the decomposition is complete

`python -m tests.module_ownership --check-complete` exits 0: 7736 tracked files, 7735
owned by modules, 1 pinned vendored data (the o200k_base vocabulary, covered by
`PINNED_VOCABULARIES`, see `2026-11-11-the-tokenizer-vocabulary-is-pinned-data.md`),
0 unassigned. 1722 module packets each measure at most 100000 o200k_base tokens;
the largest are `lsp-protocol-tests` (99968) and
`test-workspace-revision-hashing-and-git-bounds` (99883). Files too large for a packet
became facades over concern modules, large test files were split by concern, and
providers reached only through public names are carried as qualified interface
descriptions. `tests/test_module_registry_integration.py::test_every_tracked_file_belongs_to_a_module`
fails on any tracked file left without an owner, so the completion cannot regress
silently. Headroom is small in the largest packets and in `module-map`, which owns
`docs/MODULES.md` and grows with every module.
