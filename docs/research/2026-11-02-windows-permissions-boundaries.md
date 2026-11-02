# Windows permissions: убрать зависимость базового IO от очереди и транзакций

Дата исследования: 2026-11-02. Статус: оба последовательных шага явно согласованы пользователем.
Исходная версия: dd3ad044. Это следующая граница общего плана декомпозиции.

## Подтверждённая причина связанности

reliable_memory._harden_runtime_owner_only импортирует queue._harden_owner_only,
который на Windows импортирует transaction._harden_windows_acl.
reliable_memory._require_windows_owner_only импортирует queue._is_owner_only,
а проверка Windows в очереди снова импортирует transaction helpers.
Ещё один strict Windows verifier reliable_memory импортирует transaction напрямую.
Таким образом базовый runtime IO зависит от исполнителей, которые сами его используют.
Это подтверждённая структура зависимостей, не утверждение о неизбежном import crash.

ACL-код использует _windows_kernel32 для console code page. Тот же loader нужен
операциям над файловыми handles, а его прототипы ссылаются на ctypes structures.
Простой перенос ACL с обратным импортом transaction не устранит исходную связь.

## Два последовательных переноса

1. scripts/llm_wiki/windows_native.py получает из markdown_transaction
   _WindowsFileInformation, _WindowsDispositionInformation,
   _WINDOWS_KERNEL32_PROTOTYPES и _windows_kernel32 вместе с текущим lru_cache.
   Зависимости только ctypes/wintypes и functools. Импорт не загружает kernel32:
   WinDLL вызывается лениво, как сейчас. Старые имена в transaction — identity aliases.
   Операции открытия, проверки, удаления и закрытия handles остаются у transaction.
2. scripts/llm_wiki/windows_acl.py получает 18 ACL-функций transaction от
   _windows_acl_identity до _acl_lines_naming и четыре функции очереди:
   _windows_acl_lines, _acl_owner_lines, _acl_is_owner_only, _is_owner_only_windows.
   Новый модуль использует только stdlib и windows_native. Обратные local imports
   transaction в перенесённых функциях удаляются, они вызывают своего владельца.
   reliable_memory обращается к новому owner напрямую во всех трёх Windows путях;
   queue и transaction сохраняют их разные POSIX/Windows dispatch semantics.

Точный список определений: logs/windows-permissions-inventory.json.
Все подтверждённые production imports этих helper names переводятся на владельца
с сохранением прежнего lazy/eager scope; compatibility aliases остаются там, где
есть действующие потребители. Подмены внутренних вызовов тестами переносятся к
новому владельцу; test patch внешнего dispatch в очереди остаётся действительным.
Не добавлять forwarding registry, динамическую подмену globals или дубли loader.
Старые определения удалить после AST/behavior сравнения.

У каждого модуля собственные tests/modules/windows_native либо windows_acl,
guide и context manifest в docs/modules/windows-native* либо windows-acl*.
Полный пакет каждого ≤100000 o200k_base токенов; числа пока не измерены,
поскольку новые модули и их собственные тесты ещё не созданы.

## Что сохраняется и почему

Пять kernel32 prototypes, размеры handles, use_last_error=True, ctypes structures
и единственный cache остаются прежними. Сохраняются имя пользователя/domain,
порядок codec fallbacks и работа с не-ASCII именами, icacls arguments, timeout 5 с,
все тексты ошибок и fail-closed результаты. Новые лимиты не вводятся.

Strict verifier требует единственную явную ACE без inherited marker. Boolean
queue checker имеет отличающийся существующий контракт. Не объединять эти
предикаты и не усиливать/ослаблять их молча. POSIX chmod, unsupported-filesystem
warning, path/symlink/handle checks, transaction fences, runtime roots и schema
остаются прежними. Не менять ACL живых файлов ради демонстрации переноса.

Альтернативы: оставить обратные imports — сохраняет проблему; скопировать loader —
два владельца ABI/cache; новый framework/pywin32 — лишняя зависимость и риск
изменения поведения. Объединить весь transactional Windows IO с ACL означало бы
переносить операции публикации ради permission-check. Выбраны два существующих
слоя: общий ленивый binding и проверка/применение ACL над ним.

## Первичные источники, проверенные 2026-11-02

- [Microsoft icacls](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls):
  grant:r заменяет explicit permissions, inheritance removal имеет отдельную
  семантику; /verify не доказывает сам по себе owner-only. Поэтому команды и
  последующая собственная проверка сохраняются, а inherited/explicit не смешиваются.
- [Python ctypes](https://docs.python.org/3.10/library/ctypes.html): типы аргументов,
  результата и Windows error handling существенны для native calls. Сохраняются
  restype/argtypes/use_last_error и lazy loading, без platform-dependent imports
  исполняемых runtime модулей. Python 3.10 upstream unsupported, project floor не меняется.
- [OWASP insecure temporary file](https://community.owasp.org/vulnerabilities/Insecure_Temporary_File):
  временные имена и разрешения не устраняют гонку создания; secure create/open
  нужны отдельно. Поэтому ACL extraction не подменяет существующие filesystem
  containment/atomic-create проверки и не заявляется новым OS sandbox.

Дополнительно [Python os.chmod](https://docs.python.org/3.10/library/os.html#os.chmod):
платформенные ограничения различаются; POSIX mode bits не считаются заменой Windows ACL.
Источники обосновывают сохранение контрактов, не доказывают отсутствие всех
существующих ACL-дефектов. Native Windows validation отдельно от Linux mocks.

## Исследование и квалификация

Встроенный индекс dd3ad044 актуален, changes:stale=false. Граф выявил loader callers:
console codec и пять групп handle operations; hardener callers — оба dispatch и
существующие tests; boolean Windows reader — queue dispatch и Cyrillic-owner tests.
Граф неполный; AST и source search дополняют локальные imports/monkeypatch.
Evidence: logs/windows-permissions-graph.json.

Baseline: transaction, reliable memory, non-ASCII owner и native handle-width tests
на Linux — 154 passed, 8 skipped, 3,15 с. Evidence: logs/windows-permissions-baseline.txt.
Пропуски не объявляются native Windows qualification.

После явного согласия: private decision и STRUCTURE до кода; каждый шаг отдельно
с AST comparisons (imports нормализуются), red/green boundary guard, isolated import,
identity/cache tests, faithful mocked subprocess outcomes и существующими native tests.
Проверить Unicode owner, extra ACE, malformed/failed command, inherited-vs-explicit,
unsupported codec, full-width handles и last-error preservation. Миграции consumers
проверяются вместе с queue, transaction/recovery, reliable_memory и compile_cache.
Каждый этап: Python 3.10, global CCN/shape, Ruff, CI discovery/weights, полный token
budget; commit/push и актуализация индекса. Native Windows не заявлять без запуска.
Live rollout, перестройка operational state, изменение security policy и новый
WinAPI surface не входят в согласуемые переносы.

Дополнительный baseline readers: 5 passed, 0,32с;
logs/windows-permissions-reader-baseline.txt.


## Первый шаг: native binding

До кода записаны private decision и STRUCTURE. Весь блок двух structures,
prototypes и lazy loader перенесён побайтно из dd3ad044; остальной transaction AST
совпал после исключения imports. Boundary guard сначала упал на старом владельце.
Aliases сохраняют identity типов, prototypes и единственного cache.
Own+handle integration:12passed; Python3.10 own+handles+Unicode/readers:19passed.
Первоначальный пакет:69845 o200k_base токенов/23 файла. Native Windows не запускалась.
Evidence: logs/windows-native-preservation.json; logs/windows-native-boundary-red.txt;
logs/windows-native-own.txt; logs/windows-native-python310.txt;
logs/windows-native-related.txt/.xml; logs/windows-native-final-budget.txt.
Второй шаг ACL на момент этой записи ещё не реализован.

Native binding expanded:325passed,8skipped,58,85с; включает transaction/recovery,
reliable memory, Unicode readers, global CCN/shape, structure и CI policy.
Ruff и diff checks чисты. Пропуски — не доказательство native платформы.

## Второй шаг: ACL owner

22 функции перенесены из transaction/queue; их AST совпадает после удаления
обратных imports. Остальные тела transaction, queue, archive, compile_cache,
evidence_resolver и LSP совпали без imports. В reliable_memory сравнение
дополнительно нормализует ровно два согласованных перенаправления Windows-only
вызовов: hardening и boolean verification; strict verifier меняет только import.

Boundary guard сначала поймал прежние определения и обратные imports базового IO.
После переноса owner зависит только от stdlib/windows_native, runtime IO не
импортирует queue/transaction. Старые function aliases проверены по identity;
подмены внутренних ACL вызовов перенесены к owner, внешние dispatch не менялись.
Самостоятельный non-ASCII test остаётся на прежнем пути (его fixture используется
integration tests), импортирует owner и входит в полный пакет ACL.

Первый selected набор:48passed/1skipped. Expanded:637passed/11skipped,69,75с,
включая transaction/recovery, queue/migration, reliable memory, archives, cache,
evidence resolver, global CCN/shape, structure и CI. Python3.10 selected:
119passed/4skipped,2,28с. Добавленные direct runtime bridge tests отдельно:
19passed/1skipped; их CCN/shape проверены отдельно после добавления.
LSP ACL/owner selection:1passed/2skipped/254deselected; это не весь LSP suite.
Windows native validation не проводилась. Skip не является успехом платформы.

Evidence: logs/windows-acl-preservation.json; logs/windows-acl-boundary-red.txt;
logs/windows-acl-own.txt; logs/windows-acl-related.txt/.xml;
logs/windows-acl-python310.txt; logs/windows-acl-runtime-bridge.txt/.xml;
logs/windows-acl-lsp.txt; logs/windows-acl-final-budget.txt.
Ни live ACL, ни runtime layout/schema, ни POSIX policy не изменялись.
Полный regression suite, public CI и live rollout не заявлены.
