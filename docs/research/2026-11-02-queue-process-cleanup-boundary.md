# Следующий срез очереди: остановка дерева процессов

2026-11-02. Согласовано ответом пользователя «продолжай» после предложения
конкретной границы. Решение записано до кода; результаты реализации — ниже.
Основа: 81d4c01c, индекс generation-18e454e27e69b91c-8bb023e5.

## Что предлагается

Выделить `scripts/llm_wiki/queue_process_cleanup.py`: существующее обнаружение
потомков, остановка дочернего процесса и проверка завершения его дерева.
Собственные тесты: `tests/modules/queue_process_cleanup/`. Описание и полный
манифест: `docs/modules/queue-process-cleanup.md` и
`docs/modules/queue-process-cleanup-context.json`.

Перенести без изменения тел 29 функций от `_kill_process_group` до
`_terminate_processor_child`, три константы `_TH32CS_SNAPPROCESS`,
`_MAX_PROCESS_PATH`, `_DISCOVER_DESCENDANTS` и существующий `_pid_is_alive`.
Всего 30 функций. Имена и сигнатуры сначала сохраняются; это внутренний модуль
очереди, не универсальная служба управления произвольными процессами.

Зависимости: стандартная библиотека (включая multiprocessing, typing и ctypes),
`llm_wiki.queue_contracts.QueueOperationError` и существующий
`process_liveness.pid_alive`. Импорты executor, state, capture, diagnostics,
Markdown writers и LLM в новом модуле запрещены проверками.

Запуск процесса, `_processor_child_entry`, протокол Pipe, `_ChildRun`, heartbeat,
хранилище, задачи, отчётность и блокировка очереди остаются в memory_queue.
Очередь вызывает импортированные функции владельца. Не добавлять callback
registry, прокси globals, wrappers или вторую копию алгоритмов. Старые имена,
необходимые действующим потребителям и интеграционным тестам, могут быть прямыми
aliases тех же объектов; каждое сохранённое имя требует проверки использования.
Изменение monkeypatch старого внутреннего имени не должно искусственно
перенаправляться: проверки патчат фактического владельца, сохраняя утверждения.

## Основание и ограничения

memory_queue сейчас занимает 135 175 o200k_base токенов. Тела выбранных
определений и констант — 3 245 токенов, без окружающих комментариев, импортов,
тестов и документов. Это **не** размер будущего полного пакета и не обещание
сделать всю очередь меньше 100 000 одним переносом.

Встроенный граф подтверждает зависимость cleanup от QueueOperationError и
локальных функций проверки/остановки. MCP dependencies ответил timeout из-за
занятых workers; тот же встроенный code_graph прочитал актуальную генерацию
из отдельного процесса. Граф неполон, поэтому граница дополнительно проверена
по исходникам и AST/symtable. Совпадения имён в workspace_revision и sync_memory
являются их собственными функциями, а не потребителями функций memory_queue.
Их объединение в этот модуль не предлагается.

Инварианты: неизвестный Windows snapshot не равен пустому дереву; отказ сигнала
не равен подтверждённому завершению; исчезнувшая после SIGTERM группа допускает
успех только при сохранении остальных проверок; живой потомок блокирует успех.
Порядок handshake сохраняется: дерево читается после выполнения задания, до
остановки процесса. Сохраняются существующие deadlines, интервалы и ошибки.
Новых лимитов, runtime paths, зависимостей и фоновых служб нет.

Извлечение не усиливает OS containment: это прежняя очистка доверенных локальных
процессов, не sandbox и не защита от намеренного побега процесса. Linux-проверки
не доказывают работу native Windows/macOS. Живая установка не заменяется всей
незавершённой рабочей веткой.

## Альтернативы и источники

Оставить весь subprocess runner вместе с диагностикой означает сохранить
обратные зависимости на executor. Перенести только функцию terminate означает
оставить её алгоритм разбросанным между владельцами. Заменить алгоритм на один
Process.terminate теряет потомков. Ввести psutil или объединить все платформенные
kill helpers сейчас означает менять поведение и зависимости без необходимости.
Выбран узкий существующий алгоритм с единственным владельцем и сохранённым
контрактом; это вывод для данного проекта, не универсальная лучшая архитектура.

Проверены три независимых первичных источника 2026-11-02:

- [Python multiprocessing](https://docs.python.org/3.10/library/multiprocessing.html):
  terminate не останавливает потомков; принудительная остановка может повредить
  pipe/queue. Поэтому запуск и обработка результата остаются у очереди, а
  проверка дерева сохраняется. Страница помечает 3.10 как неподдерживаемую
  upstream; проектный compatibility floor этим переносом не меняется.
- [Microsoft Toolhelp](https://learn.microsoft.com/en-us/windows/win32/toolhelp/taking-a-snapshot-and-viewing-processes):
  snapshot обходится через Process32First/Next, handle закрывается. Существующее
  различение конца списка и ошибки необходимо сохранить.
- [Linux kill(2)](https://man7.org/linux/man-pages/man2/kill.2.html): signal 0
  проверяет существование и права; успешная отправка группе не доказывает смерть
  всех её процессов. Отдельная проверка завершения остаётся обязательной.

## Порядок реализации после согласования

1. Записать частное decision и обновить STRUCTURE до переноса кода.
2. Зафиксировать AST определений, константы, defaults и исходные результаты
   связанных тестов. Доказать исходную зависимость unit tests от executor.
3. Перенести алгоритм, удалить старые тела, проверить идентичность AST и
   единственность sentinel. Перевести unit tests на владельца без ослабления
   утверждений. Сохранить настоящие worker/grandchild сценарии интеграционными.
4. Проверить импорт без executor, негативные import guards, ошибки snapshot,
   permissions, потомков и deadlines. Выполнить реальные spawn/grandchild,
   timeout/cancellation, блокировку задания при cleanup failure, Python 3.10,
   Ruff, измерение сложности и structural/CI discovery проверки.
5. Измерить полный maintenance packet с исходниками, тестами, данными,
   инструкциями, историей и необходимыми контрактами. Соблюсти 100 000 токенов
   без скрытого исключения необходимого контекста. Интеграционные сценарии
   учитывать отдельно и запускать, не выдавая их за независимые unit tests.
6. Обновить встроенный индекс, проверить legacy consumers, сохранить результаты.
   Не объявлять весь проект модульным или экономию полного цикла LLM без замеров.

Evidence: logs/process-cleanup-graph.json, process-cleanup-closure.json,
process-cleanup-proposal-inventory.json, process-cleanup-baseline.txt;
scripts/memory_queue.py; tests/test_memory_queue_cli.py;
tests/test_memory_queue_races.py; tests/test_already_dead_group_is_not_a_cleanup_failure.py;
tests/test_a_childs_tree_is_read_when_it_is_killed.py.

## Исходная проверка до переноса

На 81d4c01c выбранные cleanup/descendant/snapshot/tree сценарии в четырёх
перечисленных test files: **20 passed, 1 skipped, 76 deselected**, 79,27 с,
Python 3.12.3, Linux. Пропущена native Windows snapshot проверка. Это исходный
выбранный набор, не квалификация будущего переноса и не полный suite. Селекция:
`cleanup or descendant or snapshot or tree or pid_probe or already_dead or
refused_signal or delivered_signal or branch_is_skipped`.

## Реализация и квалификация

После согласования решение записано в private queue-process-cleanup-decision-20261102,
STRUCTURE обновлён до переноса. Все 30 AST функций и три константы совпали
с 81d4c01c. Оставшийся executor AST совпал после исключения импортов и переноса.
22 определения тестов/вспомогательных объектов совпали после замены имени
модуля-владельца. Исходные assertions и skip conditions сохранены; старый
test_already_dead_group_is_not_a_cleanup_failure.py теперь является собственным
tests/modules/queue_process_cleanup/test_group.py, а unit tests cleanup/snapshot
из CLI/races находятся в том же пакете. Интеграционные сценарии остались на месте.

Boundary guard сначала упал на трёх файлах, импортировавших memory_queue, затем
прошёл. Fresh-interpreter test запрещает executor/state/writer imports и проверяет
probe, tracking, cleanup verdict и идентичность sentinel. Четыре необходимых
имени очереди — прямые aliases, что отдельно проверяется тестом. Других старых
тел или прокси не осталось. Signal import удалён как больше не используемый.

Расширенный Linux-набор Python 3.12: **349 passed, 1 skipped**, 150,15 с.
Он включает module/queue/CLI/races, реальные spawn/grandchild, lease loss,
cancellation, startup death, blocked task, liveness, structure, CI и общий
контроль CCN/форм ветвления. Новый alias identity test прошёл отдельно после
collection этого набора. Python 3.10: **38 passed, 1 skipped** собственных и
**77 passed** интеграционных. Windows snapshot пропущен на Linux. Ruff прошёл;
максимальная измеренная CCN нового production модуля — 5.

Семь бюджетных пакетов проверены: 71 passed, 265 deselected. На момент этого
прогона queue-process-cleanup занимал 82 670 токенов / 29 файлов; окончательный
размер после добавления этого отчёта измеряется повторно. Исходник очереди
131 955 токенов, новый исходник 3 346. Это не завершение декомпозиции всей
очереди и не доказательство снижения полного расхода LLM. Обновлены измеренные
CI weights перенесённых test files; удалён вес исчезнувшего старого файла.

Evidence: logs/queue-cleanup-preservation.json; logs/queue-cleanup-boundary-red.txt;
logs/queue-cleanup-boundary-green.txt; logs/queue-cleanup-related.txt и .xml;
logs/queue-cleanup-python310-own.txt; logs/queue-cleanup-python310-related.txt;
logs/queue-cleanup-complexity.json; logs/queue-cleanup-budgets.txt.
Полный suite нового HEAD, remote CI, native Windows/macOS и установка здесь
не заявляются. Предыдущий полный прогон относится к baseline до этого переноса.
