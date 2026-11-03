# Queue heartbeat

Владелец: `scripts/llm_wiki/queue_heartbeat.py`. Два существующих класса владеют
потоком, сигналом остановки и ошибкой продления. Source fence дополнительно
защищает текущее значение блокировкой. Узкие Protocol описывают необходимые
методы очереди; SQL, fencing и реализация ожидания остаются у очереди.

Зависимости: стандартная библиотека, queue_contracts, reliability_defaults и
lease_renewal. Импорт исполнителя, storage или durability запрещён. Старые
memory_queue._LeaseHeartbeat и _SourceFenceHeartbeat — те же объекты классов,
поэтому существующие подмены stop сохраняют действие. Stop floor принадлежит
новому модулю; тесты меняют его там. Это внутренний параметр, не настройка.

Значения, время начала владения, интервалы, budget busy timeout, названия daemon
threads, lock, порядок публикации ошибки и bounded join сохранены. После join
проверяется is_alive; живой поток вызывает heartbeat_stop_timeout. Убийство
процесса по-прежнему не гарантирует graceful cleanup daemon thread.

Полный пакет указан в queue-heartbeat-context.json: исходники зависимостей,
контракты, документация и все собственные тесты. Предел — 100000 o200k_base
токенов. Тесты запускаются отдельно: `pytest tests/modules/queue_heartbeat -q`.
Интеграция дополнительно проверяется тестами очереди, compile controller,
busy database, потери аренды и прекращения дочернего выполнения.

Evidence: docs/research/2026-11-02-queue-renewal-boundaries.md.

## Reliability defaults and queue heartbeat (approved 2026-11-02)

Shared canonical contract: [reliability-defaults](reliability-defaults.md).
The complete guide is included in this owner's maintenance packet.
