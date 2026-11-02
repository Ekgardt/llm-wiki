# Queue timing: нормализация времени и расчёт повторной попытки

Дата: 2026-11-02. Статус: явно согласовано пользователем. Исследование: 1a1f50b4; перенос: после исправления 2ea7fa24.

## Предлагаемый перенос

Новый scripts/llm_wiki/queue_timing.py владеет существующими _as_utc,
_timestamp, _parse_timestamp, восемью функциями чтения Retry-After и
_retry_available_at. Вместе с ними переносятся _RETRY_AFTER_READERS,
_MAX_RETRY_AFTER_SECONDS и _RETRY_RANDOM. Всего 12 функций, без изменения тел.
Зависимости: datetime, email.utils, math, random и уже выделенный DEFAULTS.
Никаких imports очереди, storage, SQLite, filesystem/network IO.

_utc_now остаётся у очереди: чтение текущего времени — источник значения,
а модуль вычисляет результат из переданного now. Таким образом текущие
monkeypatch часов в corruption/capture tests продолжают работать.
Очередь сохраняет SQL, число попыток и переходы ready/dead/blocked;
MemoryQueue._retry_after_seconds остаётся существующим compatibility методом.
Прежние function names доступны через прямые identity aliases. В тесте
failed capture monkeypatch _RETRY_RANDOM переносится к новому владельцу;
перенаправление globals или повторные генераторы случайных чисел не добавляются.

Сохраняются naive-as-UTC, timezone conversion, microsecond formatting,
None parsing, порядок bool-before-int, HTTP-date parsing, семидневная граница,
full-jitter, DEFAULTS и max(jitter, Retry-After). Для не-ready возвращается
тот же now без вычисления задержки. Политика legacy/adopted queue не объединяется:
legacy использует свою configurable random/retry policy, adopted — свою текущую.
Новых лимитов, таймеров, daemon, schema/env/runtime-path изменений нет.

## Альтернативы и основания

Оставить всё в очереди можно, но это сохраняет executor dependency для расчётов
над значениями. Копия _as_utc в retry-модуле нарушит единственного владельца.
Отдельные модули для каждой из трёх небольших функций времени увеличат число
границ без независимой ответственности. Новый общий scheduler, Tenacity/asyncio
или изменение стратегии jitter не нужны для сохранения текущей семантики.
Выбран один небольшой модуль временных значений очереди, без выполнения работы.

Исследование первичных независимых источников 2026-11-02:

- [RFC9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3):
  Retry-After допускает HTTP-date либо целое неотрицательное число секунд.
  Текущий API дополнительно принимает datetime/float; это существующий контракт,
  его не сужать до HTTP grammar при структурном переносе.
- [AWS Exponential Backoff And Jitter](https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/):
  сравнивает full/equal/decorrelated jitter; full jitter снижает совместную
  нагрузку относительно повторов без jitter. Это исследование 2015 года,
  не основание менять текущую стратегию или обещать ускорение без замеров.
- [Python 3.10 datetime](https://docs.python.org/3.10/library/datetime.html):
  naive datetime не определяет timezone сам; интерпретация naive как UTC здесь
  именно контракт приложения, а не поведение, гарантированное стандартом.
  Python 3.10 upstream unsupported, project floor сохраняется и проверяется.

## Доказательства и план проверки

Индекс 1a1f50b4 актуален (changes:stale=false). Встроенный code_graph из нового
процесса исследовал callers _as_utc, _retry_after_seconds и _retry_available_at;
граф неполный, исходники дополнили импорты, текущую legacy обвязку и monkeypatch.
Evidence: logs/queue-timing-graph.json; logs/queue-timing-inventory.json.
Baseline memory_queue, adopted Retry-After, small integrity gaps и failed capture:
127 passed, 2,96с. Evidence: logs/queue-timing-baseline.txt.

После согласования записать private decision и STRUCTURE до кода. Перенести
тела без изменений и подтвердить AST; удалить прежние определения. Добавить
tests/modules/queue_timing, docs/modules/queue-timing.md и context manifest.
Own tests: timezone/naive/None/microseconds; bool, negative, NaN/Inf, HTTP-date,
past/future/invalid Retry-After; точные границы jitter на управляемом generator;
не-ready не обращается к generator; изолированный import без runtime.
Boundary guard должен падать до переноса, потом проходить. Интеграция проверяет
aliases, прежние подмены часов, legacy/adopted retries и независимый следующий capture.

Финальный пакет включает все own tests, документы, инструкции и исходник DEFAULTS;
100000 o200k_base — потолок владельца, не оценка размера ещё не созданного пакета.
Ruff, реальные global CCN/shape, CI weights/discovery, Python 3.10 и связанные tests
обязательны. После commit/push обновить индекс и private log. Live rollout,
полный suite и native платформы не заявлять без фактического выполнения.

Установленная система остаётся на e95f6a4a; её известные проблемы nightly/capture
не решаются этим переносом. Candidate worktree отдельный, private knowledge не
публикуется. Размер исходника очереди до этого шага 120706 токенов; этот перенос
сам по себе не завершает общую декомпозицию.

## До переноса обнаружена ошибка числового Retry-After

Исследование крайнего ввода на исходной 1a1f50b4 воспроизвело ValueError для
строки `²` и OverflowError для целых ±10**400. Unicode isdigit допускает символы,
которые float не читает; numeric conversion выполнялся вне защищённого участка.
Второй случай вызван преобразованием произвольного Python int в ограниченный
float до применения существующей границы. Ошибка возникает при queue.fail,
так что переход из leased не завершается. Это существующий дефект, не результат
переноса. Воспроизведение: logs/queue-timing-edge-baseline.json и
logs/queue-retry-numeric-red.txt (6 failed, 7 passed, 112 deselected).

Отдельное исправление в текущем memory_queue.py не меняет структуру:
целое число ограничивается до float, а conversion numeric text включено
в уже существующий обработчик ошибок parsing. Огромное положительное целое
получает прежний cap 604800 секунд; отрицательное остаётся недопустимым;
непреобразуемая строка не задаёт Retry-After. Остальные finite/NaN/Inf и HTTP-date
правила сохраняются. Новый лимит не добавлен. Для rejected задержки остаётся
прежний jitter, отказ задачи не маскируется успешным выполнением.

Регрессии проверяют общий parser и обе реализации очереди: task выходит из leased
в ready, снимает lease token, сохраняет failed attempt и соблюдает срок повторения.
Проектный тест независимого следующего capture включён. На момент отдельного исправления структурный перенос
queue_timing ожидал согласия; его AST baseline должен быть взят после
этого отдельного исправления, а не притворяться совпавшим с неисправной версией.


Квалификация отдельного исправления: 147 passed, 36,06с (включая global CCN/shape);
Python 3.10: 136 passed, 3,43с. Ruff чист. Evidence:
logs/queue-retry-numeric-related.txt/.xml; logs/queue-retry-numeric-python310.txt.
Это не полный suite, native Windows/macOS и не live rollout.


## Согласованный перенос

Получено явное «Да, реализовать эту границу»; private decision и STRUCTURE
записаны до кода. Индекс 2ea7fa24 актуализирован до переноса. Сохранены AST всех
12 функций и трёх констант, остаток queue AST совпал после исключения imports.
Boundary guard сначала упал на прежних определениях, после переноса прошёл.
Единственная подмена _RETRY_RANDOM перенесена к владельцу, _utc_now остался
в memory_queue. Старые function names проверяются на identity aliases.

48 own tests прошли: UTC/offset/microseconds/None, сохранённые и исправленные
Retry-After cases, детерминированные крайние точки jitter и cap, stated floor,
non-ready без RNG и isolated interpreter без executor/storage imports.
Первоначальный полный пакет:72065 o200k_base токенов/26 файлов.
Evidence: logs/queue-timing-preservation.json; logs/queue-timing-boundary-red.txt;
logs/queue-timing-own.txt; logs/queue-timing-related.txt/.xml;
logs/queue-timing-python310.txt; logs/queue-timing-final-budget.txt.


Квалификация переноса: 301 passed, 64,19с (own, queue, migration, corruption,
retry/capture, global complexity/shape, structure и CI); Python 3.10:
182 passed, 3,82с (own, queue, adopted retry и failed capture).
Ruff и git diff --check прошли. AST функций совпал и после форматирования imports.
Все 12 полных пакетов ниже 100000; memory_queue.py теперь 119996 токенов.
Финальный бюджет и CI weights проверены отдельно. Дубли определений удалены;
старые function names нужны compatibility surface, RNG alias не сохраняется.
Live installation, полный regression suite и native платформы не квалифицировались.
