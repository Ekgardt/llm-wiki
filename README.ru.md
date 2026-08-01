# LLM Wiki

[![Tests](https://img.shields.io/badge/tests-1916%20collected-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-3.4.0-blue.svg)](CHANGELOG.md)

**Локальная система памяти для AI-агентов. Markdown-файлы, версионирование в git, без облачных зависимостей.**

LLM Wiki даёт каждому AI-агенту, которым вы пользуетесь — OpenCode, Codex, Claude Code, Cursor, Antigravity — общую постоянную базу знаний, которая сохраняется между сессиями. Система фиксирует то, что вы и агенты обсуждали, компилирует из транскриптов сессий устойчивые страницы знаний и инжектирует нужный контекст в начале каждой сессии — чтобы вам не приходилось заново объяснять одно и то же.

Всё хранится на вашем диске в виде обычного markdown: читается в Obsidian, сравнивается в git, полностью принадлежит вам.

**Языки:** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## Содержание

- [Как это работает](#как-это-работает)
- [Возможности](#возможности)
- [Быстрый старт](#быстрый-старт)
- [Подключение агентов](#подключение-агентов)
- [Архитектура](#архитектура)
- [Бенчмарк](#бенчмарк)
- [Сравнение](#сравнение)
- [Участие в разработке](#участие-в-разработке)
- [Благодарности](#благодарности)
- [Лицензия](#лицензия)

---

## Как это работает

```
Вы работаете в AI-агенте как обычно (OpenCode / Codex / Claude Code / Cursor)
             ↓
Хуки незаметно фиксируютbreadcrumbs + классифицируют сессии (FLUSH_MAJOR/MINOR/OK)
             ↓
Фоновая компиляция превращает daily-логи в устойчивые страницы знаний
(с VERIFY-BEFORE-WRITE — цитаты проверяются, не доверяются LLM на слово)
             ↓
Следующая сессия: guardrails + advisory + метакогнитивный контекст инжектируются
             ↓
Агент продолжает с того места, где вы остановились — без повторных объяснений
```

Система следует паттерну «компилируй, а не извлекай» ([Karpathy, апрель 2026](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)): сырые сигналы сессий фиксируются в реальном времени, затем фоновый LLM-проход компилирует их в структурированные страницы знаний, вместо того чтобы полагаться на raw-retrieval в момент запроса.

---

## Возможности

### Пайплайн захвата
- **5 хуков Claude Code**: SessionStart, PreCompact, SessionEnd, UserPromptSubmit, PostToolUse — полное покрытие жизненного цикла
- **Плагин OpenCode** (JS) — официальный lifecycle, атрибуция проекта сначала по `worktree`, затем по `directory`, захват промптов, автоматическая инъекция контекста, нативные tools и авторизованная SDK-компиляция
- **Нативные хуки Codex** — `merge_codex_hooks.py` устанавливает кроссплатформенный lifecycle-capture; сохранённая Windows-обёртка служит только compatibility/exit-capture fallback
- **3-уровневая классификация сессий**: FLUSH_MAJOR (решения/уроки → запускает компиляцию), FLUSH_MINOR (гэтчи → только сохранить), FLUSH_OK (болтовня → пропустить)
- **Non-LLM breadcrumbs** — промпты по-прежнему фиксируются, а tool-breadcrumbs создаются только для прямых инструментов изменения файлов; read, search и shell их не создают, а цели и данные о происхождении ограничиваются по размеру и очищаются от секретов
- **Redaction секретов** — API-ключи, токены, длинные base64 вычищаются до любой записи

### Пайплайн компиляции
- **JSON-протокол компиляции** — не требует tool-use агента, работает с любым LLM-бэкендом
- **VERIFY-BEFORE-WRITE** — детерминированная проверка цитат на стороне Python; LLM не может сфабриковать улики
- **Детерминированный admission** — до журналирования или мутации Python требует цитаты из устойчивых разделов и соблюдение lifecycle/word-границ, затем отклоняет дубликаты в live-corpus и текущем плане; счётчики провайдера не обходят admission, неоднозначные пары только попадают в отчёт
- **Семантический дедуп** — update предпочтительнее create; авто-supersede при противоречии
- **Инкрементальность** — SHA-256 хеширование; рекомпилируются только изменённые daily-логи
- **Безопасность при конкуренции** — один удерживаемый ОС lock-файл сериализует компиляцию на Windows и POSIX; завершение процесса освобождает владение блокировкой
- **Ограниченное журналируемое восстановление** — значимые timestamp-блоки компилируются в пределах настраиваемого бюджета промпта; принятые планы и прогресс операций сохраняются до изменения заметок, а ограниченные journals/manifests могут реактивироваться из неразрушающих retired-хранилищ и правдиво отказывать при достижении квот
- **Правдивое завершение** — daily-лог помечается скомпилированным только после фиксации всех закреплённых batch, успешной перестройки Markdown-индекса и публикации самопроверяемой effect receipt; устаревшая или отсутствующая receipt отменяет доверие и запускает replay
- **Персистентная очередь** — монотонный FIFO-порядок среди eligible-задач, межпроцессные leases, ограниченные повторяемые проходы и продолжения, пока остаётся eligible-работа, восстановление stale lease через 10 минут, retry-backoff 60 секунд и предел в 5 попыток; сохранённая provenance задачи применяется один раз, а legacy-provenance остаётся явной

### Поиск и извлечение
- **Triple-fusion search**: BM25 (FTS5) + Vector (sentence-transformers) + Graph-neighbor (wikilink RRF)
- **Взвешенный RRF**: BM25=2.0, Vector=1.0, Graph=0.5 — предотвращает регрессию на known-item запросах
- **Title + filename boost** — точное совпадение имени файла даёт rank 1 сразу
- **Typed-provenance ранжирование** — `source_authority: user` выше, чем `ai-derived` / `inferred`
- **Темпоральные запросы** — `--as-of YYYY-MM-DD` фильтрует по `valid_to` frontmatter
- **3-уровневая стратегия** — DIRECT (<50 страниц, только индекс), HYBRID (50–300, +QMD), QMD (>300)

### Проактивный интеллект
- **Guardrails** — авто-инжекция выученных корректировок на SessionStart (предотвращает повторение ошибок)
- **Advisory** — поднимает открытые треды, последнее решение, lint-алерты, кросс-проектные инсайты
- **Метакогнитивный контекст** — инвентаризация vault, backlog компиляции, распределение flush-tier
- **Проектно-безопасный daily-контекст** — timestamp-блоки и legacy-сводки в heading/bullet формате нормализуются в памяти; приоритет получают устойчивые сводки и пользовательские промпты совпадающего проекта, tool-breadcrumbs исключаются
- **Захват обратной связи** — обнаруживает корректировки/предпочтения в транскриптах, сохраняет как кандидаты на промоутер

### Мультипроект и мультиагент
- **Один vault, много проектов** — 5-шаговая collision-safe slug-система, per-project `state.md`
- **Bootstrap проектов** — авто-генерация контекста из git-истории, README и tech-стека с показом на следующем SessionStart
- **Blackboard-протокол** — параллельные агенты клеймят задачи, сигналят завершение, детектят конфликты
- **Loop-детектор** — фиксирует циклические редактирования (fix → review → redo)
- **Agent timeline** — атрибуция: какой агент какое решение принял и когда

### Обслуживание
- **14 lint-проверок (13 структурных + 1 LLM-оцениваемое противоречие)** — битые wikilinks, orphan'ы, missing frontmatter, невалидные supersede-цепочки, temporal validity, gap'ы, sparse pages, missing sources, противоречия
- **Type-aware архивация** — debugging 60 дн, patterns 180 дн, decisions никогда
- **Правдивый nightly-статус** — ошибки очереди, компиляции, lint или производных индексов, а также оставшаяся работа дают ненулевой код выхода
- **Weekly-обслуживание под единым lease** — один maintenance-lock охватывает nightly-работу, OKF-миграцию, безопасный отчет о неисправимых задачах очереди без их удаления, архивацию, опциональную проверку противоречий и финальную перестройку Markdown/FTS/graph
- **Аудируемый repair установленного vault** — отдельные этапы audit, проверенного backup-only, привязанного к manifest apply и verify; cleanup не считается выполненным, пока оператор не проверит и не запустит эти этапы
- **OKF v0.1 frontmatter** — поля `type`, `confidence`, `source_authority`, `supersede`; авто-миграция с legacy-страниц

### Инфраструктура
- **5 LLM-бэкендов** (авто-детекция): OpenCode → Codex → Claude CLI → OpenAI → Ollama
- **Точный сервисный контракт OpenCode** — в SDK-only режиме классификация, компиляция и отложенная очередь используют `openai/gpt-5.6-luna`; каждая операция владеет ephemeral-сессией `memory-*`, которая в `finally` сначала abort, затем delete, без fallback на CLI или более слабую модель
- **Кросс-платформенность**: Windows, macOS, Linux, WSL2
- **Фоновое обслуживание Windows без консоли** — Task Scheduler запускает `pythonw`, а дочерние процессы обслуживания создаются без консольных окон
- **Ноль runtime-зависимостей** — базовая установка только stdlib; sentence-transformers и Cognee опциональны
- **Сбор 1916 тестов стабилен между платформами; локальная проверка Windows: 1881 пройдено, 35 пропущено**. Число пропусков зависит от доступности опциональных Bash, PowerShell и symlink; CI нацелен на Ubuntu + Windows + macOS с Python 3.10 + 3.13.
- **Pre-commit хуки**: ruff (статический анализ) + структурный lint + gitleaks (сканирование секретов)

---

## Быстрый старт

### Требования

- Python 3.10+
- git
- AI-агент, которым вы уже пользуетесь (OpenCode, Codex, Claude Code, Cursor или Antigravity)

### Установка (одна команда)

**macOS / Linux / WSL2:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

> **Примечание для production:** Указанные выше URL ветки `main` могут изменяться. Для production- или аудируемых развёртываний используйте URL конкретного release-тега, например `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v3.4.0/install.sh`.

Установщик:
1. Проверяет требования (Python 3.10+, git)
2. Устанавливает `uv` (быстрый Python-менеджер пакетов), если отсутствует
3. Синхронизирует зависимости (`uv sync`)
4. Запускает тестовый набор (1916 собирается на всех платформах; локально на Windows: 1881 пройдено, 35 пропущено; число пропусков зависит от shell/symlink)
5. Устанавливает переменную окружения `LLM_WIKI_ROOT` (user scope)
6. Создаёт gitignored runtime-директории (`cache/` и logs заменяемы; `run/` хранит устойчивое состояние автоматизации и восстановления)
7. Регистрирует плановое обслуживание (cron на Unix, Task Scheduler на Windows)
8. Детектирует ваших агентов и подключает их
9. Строит FTS5 search-индекс

### Ручная установка

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync
uv run pytest -q          # 1916 собрано; локально Windows: 1881 пройдено, 35 пропущено; skips зависят от среды
```

### Проверка работы

```bash
uv run python scripts/search_memory.py "auth"
uv run python scripts/lookup_mode.py
```

---

## Подключение агентов

LLM Wiki авто-детектирует установленных агентов во время установки. Вот что подключается:

| Агент | Интеграция | Как |
|-------|------------|-----|
| **OpenCode** | JS-плагин | Устанавливается в effective `$XDG_CONFIG_HOME/opencode/plugins/`; при пустом/невалидном XDG используется `~/.config`, а Windows также получает этот compatibility path, если он отличается |
| **Codex** | Нативные lifecycle hooks | `merge_codex_hooks.py` кроссплатформенно объединяет хуки с `~/.codex/hooks.json` и создаёт backup; проверка через `/hooks`. Windows-обёртка, если установщик её сохраняет, служит только compatibility/exit-capture fallback. |
| **Claude Code** | settings.json хуки | Мержится в `~/.claude/settings.json` (5 хуков: SessionStart, PreCompact, SessionEnd, UserPromptSubmit, PostToolUse) |
| **Cursor** | Rules-файл | Скопируйте `integrations/cursor/rules/llm-wiki.mdc` вручную |
| **Antigravity** | AGENTS.md snippet | Скопируйте `integrations/antigravity/AGENTS.md` вручную |
| **Obsidian** | Web Clipper template | Импортируйте `integrations/obsidian/Article-to-Inbox.json` |

Все агенты используют общий vault — решение, записанное Cursor, видно OpenCode в следующей сессии.

`XDG_CONFIG_HOME` определяет effective-каталог конфигурации OpenCode, только если
содержит абсолютный путь. Пустые и относительные значения игнорируются. На Unix
устанавливается один effective destination; на Windows также используется
`~/.config/opencode`, если нормализованные пути различаются.

### Опционально: семантический поиск

Для гибридного BM25 + Vector поиска (находит семантически связанные страницы даже без совпадения ключевых слов):

```bash
uv sync --extra semantic
```

### Опционально: Cognee-граф (300+ страниц)

Для entity extraction + relationship graph в масштабе:

```bash
uv sync --extra cognee
```

См. [docs/SETUP-COGNEE.md](docs/SETUP-COGNEE.md) для настройки Ollama.

---

## Архитектура

```
CODE          scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE     knowledge/{daily,notes,projects,raw,inbox,feedback}
RUNTIME       cache/  logs/  run/  cache/cognee/   (gitignored, внутри vault)
```

- **CODE** — отслеживается в git. Пайплайн, тесты, документация, навыки, правила, интеграции.
- **KNOWLEDGE** — отслеживается в git (публичные примеры). Полные пользовательские данные живут в установленном vault. Daily-логи и персональные страницы gitignored.
- **RUNTIME** — gitignored внутри vault. `cache/` можно перестроить, а `logs/` ротировать; `run/` — устойчивое состояние автоматизации и восстановления с задачами очереди, compile-журналами/manifest, checkpoints и repair-транзакциями.

Никогда не удаляйте `run/` целиком для восстановления после ошибки. Проверяйте
очередь через `memory_queue.py status|list`, повторяйте компиляцию через
`compile_memory.py` или `maybe_compile.py`, а для привязанных к manifest
repair-транзакций используйте `repair_installed_memory.py audit|apply|verify`.

Полное обоснование дизайна (7 аксиом, диаграмма архитектуры, таксономия памяти, архитектура поиска) — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Канонический reference структуры (что где живёт, env-контракты, запрещённые layout'ы) — в [docs/STRUCTURE.md](docs/STRUCTURE.md).

---

## Бенчмарк

> **Методология**: 60 known-item запроса (точное совпадение заголовка + ключевые слова из summary, не LLM-парафраз) по 34 курируемым страницам. BM25-only режим (FTS5). Измеряет «может ли система найти страницу X по её заголовку или ключевым словам из summary?» — наиболее релевантная метрика для персонального извлечения знаний. Это **не** LoCoMo или LongMemEval (multi-session conversation recall). Числа конкурентов — из других датасетов и не сопоставимы напрямую. Воспроизведите: `benchmark/run_benchmark.py`.

| Метрика | LLM Wiki v3.3 | agentmemory | Zep | Mem0 |
|---------|---------------|-------------|-----|------|
| Recall@1 | **95.0%** | n/a | n/a | n/a |
| Recall@3 | **100%** | n/a | n/a | n/a |
| Recall@5 | **100%** | 95.2% | 94.7% | 91.6% |
| Recall@10 | **100%** | n/a | n/a | n/a |
| MRR | **0.9667** | 0.882 | n/a | n/a |
| Латентность p50 | **6мс** | 14мс | 155мс | 880мс |
| Стоимость токенов/запрос | **0** | ~1900 | $$ | $$ |

100% Recall@5 достижимо на небольших курируемых датасетах; ожидайте 85–95% на 500+ страницах. Triple-fusion (BM25 + Vector + Graph) добавляет семантический recall поверх этих BM25-only чисел.

Воспроизведите: `uv run python benchmark/run_benchmark.py`

---

## Сравнение

| Возможность | LLM Wiki | agentmemory | ReMe | akitaonrails |
|-------------|----------|-------------|------|--------------|
| Markdown-first | Да | Нет | Да | Да |
| Мультиагент (3+ инструмента) | Да (5) | Да (32+ через MCP) | Только Claude | Да (12+) |
| Поддержка IDE | Cursor + Antigravity + Obsidian | Нет | Нет | Нет |
| Compile-not-retrieve | Да | Нет | Нет | Нет |
| VERIFY-BEFORE-WRITE | Да | Нет | Нет | Нет |
| Guardrails (выученные корректировки) | Да | Нет | Нет | Нет |
| Blackboard-координация | Да | Нет | Нет | Нет |
| Loop-детектор | Да | Нет | Нет | Нет |
| Agent timeline | Да | Нет | Нет | Нет |
| Feedback learning | Да | Нет | Нет | Нет |
| Ноль runtime-зависимостей | Да | Нет (Docker) | Нет (pip) | Нет (Rust) |
| Temporal validity (`valid_to`) | Да | Нет | Нет | Нет |
| Typed-provenance ранжирование | Да | Нет | Нет | Нет |

---

## Участие в разработке

Контрибьюции приветствуются. Критерий приёма — «выдерживает ли это контакт с реальным multi-agent workflow?»

См. [CONTRIBUTING.md](CONTRIBUTING.md):
- Настройка окружения разработки
- Release-чеклист (синхронизация README i18n, CHANGELOG, bump версии)
- Стандарты кодирования (ruff, pytest, pre-commit)
- Как добавить новую интеграцию агента

---

## Благодарности

- [Karpathy LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — паттерн «компилируй, а не извлекай»
- [Harrison Chase "Wiki Memory"](https://blog.langchain.dev/wiki-memory/) — agent-maintained files
- [Google OKF spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — vendor-neutral markdown knowledge format
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — паттерны capture/compact/subagent
- [VEP Semantic DNA](https://vep.live) — lifecycle confidence/supersede/temporal

---

## Лицензия

[MIT](LICENSE)
