# Следующий этап: параметры надёжности и продление аренды

2026-11-02. На базе add477d3. Пользователь явно согласовал оба шага ответом
«Да, реализовать оба шага». Решение и STRUCTURE записаны до production-изменений.

## Два последовательных переноса

1. `scripts/llm_wiki/reliability_defaults.py` становится владельцем существующего
   frozen dataclass ReliableMemoryDefaults (21 поле) и единственного DEFAULTS.
   Значения, типы, порядок полей, frozen-поведение и constructor сохраняются.
   reliable_memory переэкспортирует те же объекты; исторический
   `ReliableMemoryDefaults.__module__ = 'reliable_memory'` сохраняет pickle ABI.
   Изолированный import/construct не требует reliable_memory, но pickle вправе
   загрузить старый фасад. Перед переносом фиксируются доверенные старые образцы.
   Двенадцать найденных прямых production from-import потребителей переводятся
   на владельца в том же lexical scope; совместимые имена остаются в прежнем API.
2. `scripts/llm_wiki/queue_heartbeat.py` получает существующие _SourceFenceHeartbeat,
   _LeaseHeartbeat, _join_heartbeat_or_refuse и HEARTBEAT_STOP_FLOOR_SECONDS.
   Зависимости: stdlib, новый reliability_defaults, queue_contracts и существующий
   lease_renewal. MemoryQueue annotation заменяется узкими typing.Protocol:
   для lease нужны heartbeat и _heartbeat_wait; для source fence —
   heartbeat_source_fence и _heartbeat_wait. Это описания существующего интерфейса,
   не wrappers, runtime checks, subclasses или callbacks registry.

У каждого модуля собственные tests/modules/reliability_defaults/ и
tests/modules/queue_heartbeat/, guide и manifest в docs/modules/ с соответствующим
именем через дефис. Полный пакет каждого должен быть ≤100000 o200k_base токенов.
Оба переноса квалифицируются последовательно; провал первого не скрывается
выполнением второго. Согласование этого плана разрешает оба указанных шага.

## Почему такая последовательность

Исходник reliable_memory — 14069 токенов. Сам defaults dataclass —232 токена
без decorators/комментариев/тестов. Heartbeat определения —1066 токенов без
константы/обвязки. Эти числа не являются полными пакетами. Простой перенос
heartbeat с импортом DEFAULTS из reliable_memory оставляет зависимость от
общего durability/IO модуля и его зависимостей ради небольшого набора значений.
Выделение уже существующего immutable контракта устраняет эту причину без
копирования чисел или создания второго набора настроек.

Найдены прямые потребители DEFAULTS: archive_sessions, flush_memory, archive_daily,
repair_exhausted_queue_tasks, installed_memory_repair, operational_ownership,
integration_adapter, scheduled_weekly, compile_progress_migration,
markdown_transaction, memory_queue, private_vault_backup. AST инвентаризация
дополняется поиском qualified/aliased usages перед переносом; отсутствие
from-import само по себе не доказывает отсутствие потребителя.
Также найден qualified доступ reliable_memory.DEFAULTS в doctor.py; он входит
в миграцию потребителей, остальные операции doctor остаются у прежних владельцев.

Очередь сохраняет транзакции, проверки fencing, renew SQL и _heartbeat_wait.
Новый heartbeat лишь владеет существующими потоками, stop Event, lock и ошибкой.
Прежние имена memory_queue остаются прямыми aliases при подтверждённых вызовах.
Проверки, меняющие внутренний stop floor, патчат нового владельца; перенаправление
globals не добавляется. Проверяется identity классов для текущих monkeypatch
потребителей compile controller.

## Сохранение поведения

Не менять значения DEFAULTS, default arguments, held_since, renewal schedule,
attempt_seconds, типы/коды ошибок, блокировки, thread names и daemon flags.
Сохранить join с последующим is_alive и отказ heartbeat_stop_timeout. Busy SQLite
не объявлять потерей lease раньше существующего срока. Успех heartbeat не даёт
права обходить fencing при завершении задания. Stop failure продолжает блокировать
обычный путь завершения; source refresh хранит и передаёт исходную причину ошибки.
Запуск нового daemon, изменение runtime, schema, env, CLI и installation не входят.

Делать копии параметров в heartbeat, извлекать методы по числу строк или вводить
универсальную службу lease не предлагается. Оставить весь импорт reliable_memory
можно технически, но это сохраняет связанность, которую пользователь попросил
устранить. Менять scheduling на asyncio или заменять алгоритм сторонней системой
не требуется для этой локальной модульной границы.

## Исследование и его пределы

Проверены 2026-11-02 три независимых первичных источника:

- [Python threading](https://docs.python.org/3.10/library/threading.html):
  join возвращает None и требует is_alive для определения timeout; Event позволяет
  сигнализировать остановку. Поэтому существующий отказ и синхронизация сохраняются.
  Daemon threads могут быть оборваны при shutdown: перенос не устраняет это
  существующее ограничение и не обещает graceful cleanup при убийстве процесса.
- [SQLite busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html): busy handler
  может ждать блокировку до своего бюджета. Это объясняет необходимость учитывать
  время попытки при renewal и bounded join, не даёт основания менять значения.
- [AWS: leader election and leases](https://aws.amazon.com/builders-library/leader-election-in-distributed-systems/):
  локальная длительность и проверка права выполнять действие существенны для
  lease safety. Это первичный опыт 2019 года, не новая библиотека и не рекомендация
  внедрить распределённый сервис в локальную SQLite-систему.

Дополнительно проверен [Python pickle](https://docs.python.org/3.10/library/pickle.html):
импортируемое имя класса является частью восстановления объектов. Поэтому
сохраняется legacy module name при единственном владельце определения.
Python 3.10 upstream unsupported, но действующий project floor сохраняется и
проверяется; перенос не заявляет продление поддержки Python upstream.

Встроенный индекс add477d3 актуален. Граф видит вызовы общего join из обоих stop,
renew_until_stopped из обоих _run и из других компонентов. Class-only queries
не показали dependency edges; это не доказательство изоляции. Исходники,
аннотации, импорты и реальные tests дополняют неполный статический граф.

## Проверки до и после

Исходный набор reliable_memory, queue heartbeat stop, lease renewal и остановка
дочернего процесса при lease loss: **97 passed, 3 skipped**, 6,47 с на Linux.
Это baseline, не квалификация ещё не выполненного переноса.

После согласования: private decision и STRUCTURE до кода; старые AST и pickle
samples; красные boundary checks; миграция без дублирования; одинаковые значения,
identity и старые pickle bytes; isolated imports; реальные heartbeat threads и
stop timeout, source refresh/error; busy/expiry cases; queue/compile lock handoff,
lease-loss/cancellation, ownership и affected defaults consumers. Python3.10,
Ruff, реальная CCN/branch-shape проверка, CI discovery/weights и оба полных
token budgets обязательны. Обновить индекс и удалить заменённые тела, сохраняя
только документированные compatibility names. Живой vault не заменять всей
незавершённой candidate веткой. Native платформы и полный suite не заявлять без
их фактического выполнения.

Evidence: logs/queue-renewal-proposal-inventory.json; logs/queue-heartbeat-graph.json;
logs/queue-renewal-callers.json; logs/queue-renewal-baseline.txt;
scripts/reliable_memory.py; scripts/memory_queue.py; tests/test_queue_heartbeat_stop.py.

## Первый шаг: defaults

ReliableMemoryDefaults и DEFAULTS выделены без изменения AST класса или значений.
Тела 13 потребителей совпали после исключения imports и замены qualified DEFAULTS
в doctor на прямой импорт владельца. Остальная реализация reliable_memory также
совпала после исключения перенесённых узлов/imports. Constructor/frozen semantics,
все поля и исторические protocol-4 bytes класса/default/custom instances сохранены.
Boundary guard сначала обнаружил прежние зависимости, затем прошёл; own import
и создание экземпляра в свежем interpreter не загружают durability/queue/state.

Own packet:18passed, первоначальный размер70023tokens/23files. Python3.10 own+
reliable_memory:77passed/3skipped. Расширенный набор:646passed/8skipped,84,48с.
Окончательный размер
зафиксированы в logs/reliability-defaults-related.txt/.xml и финальном budget log.
Он включает queue, transaction/recovery, ownership, archives, scheduled weekly,
progress migration, backup, capture hooks, heartbeat, complexity, structure и CI.
Три пропуска на3.10 относятся к платформенным случаям. Источники измерений:
logs/reliability-defaults-preservation.json; logs/reliability-defaults-own.txt;
logs/reliability-defaults-python310.txt; logs/reliability-defaults-boundary-red.txt;
logs/reliability-defaults-boundary-green.txt.

Второй шаг heartbeat на момент этой записи ещё не реализован. Живая установка
не менялась; public CI, native Windows/macOS и полный suite не заявлены.
