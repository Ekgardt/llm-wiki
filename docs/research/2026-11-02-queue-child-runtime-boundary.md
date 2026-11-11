# Следующий модуль: управление дочерним выполнением очереди

2026-11-02. На базе f44e5a6b. Пользователь явно согласовал границу ответом
«Да, реализовать эту границу». Решение и STRUCTURE записаны до реализации.

## Конкретная граница

Добавить `scripts/llm_wiki/queue_child_runtime.py`, собственные тесты
`tests/modules/queue_child_runtime/`, описание и полный манифест
`docs/modules/queue-child-runtime.md` / `queue-child-runtime-context.json`.
Модуль владеет spawn lifecycle, Pipe handshake, получением/декодированием ответа,
проверками deadline/cancellation/lease loss и вызовом уже выделенной очистки.

Перенести существующие `_encode_processor_outcome`, `_send_processor_frame`,
`_PROCESSOR_BOOLEAN_FRAMES`, `_decode_deferred_frame`, `_processor_frame_failure`,
`_decode_processor_frame`, `_ChildRun`, `CHILD_LEASE_CHECK_SECONDS`,
`_await_child_message`, `_stop_child_if_done_waiting`, `_child_stop_reason`,
`_may_keep_waiting`, `_child_ready_handshake`, `_abandon_oversize_child`,
`_child_result_frame`, `_child_left_in_time`, `_settle_child_exit`,
`_require_child_start` и реализацию `_run_processor_child`.
Их существующие определения занимают 1900 o200k_base токенов, не полный пакет.

Общий существующий `_MAX_RESULT_BYTES` (16 MiB) получает единственного владельца
в существующем queue_contracts.py; очередь и транспорт импортируют его оттуда.
Значение и действующие ограничения остаются прежними. Это общий предел хранения
и передачи результата, не копия константы в каждом модуле. Проверки, которые
меняют внутренние значения monkeypatch, должны патчить фактического потребителя;
никакого перенаправления globals между модулями не добавлять.

## Как исключить обратную зависимость

`_record_processor_failure`, `_processor_result_frame` и `_processor_child_entry`
остаются в memory_queue. Последний сохраняет свой pickle/import путь, перенаправление
stdout, создание POSIX session и диагностику. Он использует импортированные
encoder/sender helpers нового владельца. `_run_processor_inline` также остаётся.

В новом модуле функция `run_processor_child` принимает обязательный keyword-only
`child_entry`: существующую top-level функцию с контрактом `(sender, processor,
task)`. Это target для multiprocessing.Process, а не callback registry.
Прежняя memory_queue._run_processor_child с прежней сигнатурой становится тонким
вызовом этой функции с `_processor_child_entry`. В ней нет второй реализации
spawn/cleanup. Это сохраняет используемую `_interruptible` идентичность runner
и текущие вызовы/параметры. Цикл импорта не возникает: транспорт получает target
от вызывающей стороны и не импортирует executor или capture diagnostics.

Собственные тесты могут запустить реальный spawn с тестовой top-level entrypoint,
не загружая queue/state/diagnostics. Интеграционные тесты обязаны отдельно пройти
с настоящей entrypoint очереди: их контекст не скрывается за этим интерфейсом.
Прежние внутренние имена сохраняются как прямые aliases только при подтверждённых
потребителях. Не добавлять wrappers для невостребованных имён.

## Наблюдаемые причины и сохраняемые свойства

Встроенный граф актуальной базы a30aed31 и исходники показали: перенос всего
child execution вместе с `_record_processor_failure` втягивает capture_diagnostics,
memory_state и operational diagnostics. Эти зависимости нельзя назвать
изолированным транспортом. Срез с явной entrypoint оставляет их у исполнительной
политики. В ходе исследования найдена и отдельно исправлена зависимость
классификации QueueOperationError от присутствия memory_queue в sys.modules
(f44e5a6b); это не реализация данного нового модуля.

Сохраняются R/A handshake после выполнения задания, tracking потомков до ACK,
возможность принять ответ на границе deadline, ошибки oversized/malformed,
остановка при cancellation/lease loss, закрытие Pipe, исключения startup и
cleanup. Значения timeout/interval не меняются. Существующая фраза в docstring
_ChildRun.stop о том, что явный None означает отсутствие потомков, неверна:
cleanup трактует его как неизвестное дерево. При переносе уточнить текст без
изменения ветвлений и подтвердить различие None/empty/sentinel тестами.

SQL, queue states, heartbeat thread, данные и runtime layout не меняются.
Нет daemon, новых зависимостей или установки всей candidate ветки в живой vault.
Сохранение поведения подтверждается тестами, не одним совпадением строк:
обязательный child_entry и тонкий прежний вход являются явными изменениями
структуры, поэтому полное AST identity для всего среза не обещается.

## Альтернативы и первичные источники

Проверены 2026-11-02:

- [Python multiprocessing](https://docs.python.org/3.10/library/multiprocessing.html):
  spawn требует импортируемых объектов; recv_bytes ограничивает целое сообщение,
  превышение делает connection нечитаемым. Сохраняются spawn и bounded bytes,
  без замены на pickle recv. Страница 3.10 помечена upstream unsupported;
  проектный compatibility floor сохраняется и проверяется отдельно.
- [Microsoft Pipe inheritance](https://learn.microsoft.com/en-us/windows/win32/ipc/pipe-handle-inheritance):
  открытая лишняя копия writer handle влияет на наблюдение конца потока.
  Порядок закрытия parent/child ends сохраняется. Это основание проверки,
  а не обещание тождественности анонимного Win32 Pipe и multiprocessing transport.
- [Cockburn, Ports & Adapters](https://alistair.cockburn.us/hexagonal-architecture):
  отделение контракта взаимодействия от внешней реализации позволяет проверять
  компонент самостоятельно. Это исходная работа 2005 года, не новая технология.
  Здесь достаточно одного параметра target, без framework или service layer.

Альтернативы: перенос всего runner оставляет тяжёлые зависимости; перенос только
codec почти не отделяет lifecycle; удаление диагностики теряет свидетельства;
глобальный registry/динамический поиск executor скрывают зависимость. Выбран
узкий параметр entrypoint с прежним adapter, поскольку он сохраняет spawn ABI
и диагностическую политику при независимом импорте транспорта.

## Приёмка после согласования

Записать decision и STRUCTURE до кода. Зафиксировать исходные wire bytes, defaults,
ошибки и существующие интеграционные тесты. Проверить isolated import и настоящий
spawn без executor, identity/default runner consumers, все прежние worker/compile
handoff, noisy stdout, grandchild, deadline-answer, lease loss, cancellation,
startup death, result limits и diagnostic trail сценарии. Измерить сложность,
CI discovery/weights, Python 3.10 и полный maintenance packet ≤100000 токенов.
Пакет включает собственные исходники/тесты/данные, инструкции, документы и
необходимые контракты зависимостей. Native Windows/macOS не заявлять по Linux.
Требуются обновление индекса и очистка заменённых реализаций после квалификации.

Evidence: logs/queue-child-boundary-graph.json;
logs/queue-child-runtime-inventory.json; scripts/memory_queue.py;
tests/test_an_answer_that_landed_at_the_deadline_is_kept.py;
tests/test_a_childs_tree_is_read_when_it_is_killed.py;
tests/test_queue_caller_cancellation.py; tests/test_compile_worker_handoff.py.

## Реализация и проверка

Решение queue-child-runtime-decision-20261102 и STRUCTURE записаны до кода.
15 helper functions сохранили AST. ChildRun отличается только исправленной
документацией. Runner совпал по AST после явно предусмотренных изменений имени,
сигнатуры и target. Оставшийся executor совпал после исключения imports, adapter
и перенесённых определений. Прежняя сигнатура adapter сохранена. 20 контрактных
типов неизменны; в queue_contracts добавлено только прежнее значение общего
предела результата. Семь определений прежнего deadline test сохранены после
замены module reference; теперь они находятся в tests/modules/queue_child_runtime/.

Boundary guard сначала обнаружил импорт executor в перенесённом тесте, затем
прошёл. Fresh-interpreter tests выполняют реальный spawn с boolean/deferred
ответами без queue/state/diagnostics в обоих процессах. Отдельный guard отвергает
повторное объявление результата limit в транспорте и проверен на shadowed import.
Собственный пакет: **40 passed** на Python 3.12 и **40 passed** на Python 3.10.

Первый расширенный запуск обнаружил удалённые автофиксом два необходимых cleanup
aliases: **1 failed, 447 passed**; на 3.10 интеграция — **1 failed, 99 passed**.
Aliases восстановлены как explicit re-exports тех же объектов, assertions не
ослаблялись. Повтор всего выбранного расширенного набора: **448 passed**, 133,69 с.
Две поздние проверки защиты общего limit прошли в собственном пакете отдельно
от collection этого расширенного запуска. Повтор интеграции Python 3.10:
**100 passed**, 54,24 с. Исходная выборка до переноса:35passed,24,97с.

Расширенная выборка включает queue/CLI/races/targeted worker, реальные процессы
и потомков, loss/cancellation, startup death, compile worker/foreground/parent-lock/
leased-entrypoint/completion, diagnostic trail, source complexity, structure и CI.
Максимальная измеренная CCN нового runtime — 5. Ruff прошёл. CI weights новых
файлов взяты из JUnit; вес прежнего deadline file удалён.

Восемь budget packets проверены:72passed/265deselected. Размер до этого отчёта
88372tokens/34files; окончательный размер измеряется после записи результатов.
memory_queue теперь130277tokens, новый runtime2026tokens. Это ещё не завершение
разделения очереди или всего проекта. Полная регрессия всего нового HEAD,
native Windows/macOS, remote CI и установка не заявляются.

Evidence: logs/queue-child-preservation.json; logs/queue-child-boundary-red.txt;
logs/queue-child-own-final.txt; logs/queue-child-python310-own-final.txt;
logs/queue-child-related.txt; logs/queue-child-final.txt и .xml;
logs/queue-child-python310-related.txt; logs/queue-child-python310-final.txt;
logs/queue-child-budgets.txt; logs/queue-child-complexity.json;
logs/queue-child-source-counts.json.
