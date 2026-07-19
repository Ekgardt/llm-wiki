# LLM Wiki

[![Tests](https://img.shields.io/badge/tests-3081%20collected-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-4.0.0-blue.svg)](CHANGELOG.md)

**Локальная система памяти для AI-агентов. Markdown-файлы, версионирование в git, без облачных зависимостей.**

LLM Wiki даёт каждому AI-агенту, которым вы пользуетесь — OpenCode, Codex, Claude Code, Cursor, Antigravity — единый MCP-first интерфейс к общей постоянной базе знаний. MCP отвечает за чтение и действия, а тонкие нативные lifecycle-адаптеры фиксируют события сессии, которые MCP не видит. Знания сохраняются между сессиями, поэтому вам не приходится заново объяснять одно и то же.

Всё хранится на вашем диске в виде обычного markdown: читается в Obsidian, сравнивается в git, полностью принадлежит вам.

**Языки:** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## Содержание

- [Как это работает](#как-это-работает)
- [Возможности](#возможности)
- [Быстрый старт](#быстрый-старт)
- [Подключение агентов](#подключение-агентов)
- [Архитектура](#архитектура)
- [Поколения evidence и миграция](#поколения-evidence-и-миграция)
- [Бенчмарк](#бенчмарк)
- [Сравнение](#сравнение)
- [Участие в разработке](#участие-в-разработке)
- [Благодарности](#благодарности)
- [Лицензия](#лицензия)

---

## Как это работает

```
Агент читает память и выполняет действия через локальный MCP-сервер
             ↓
Тонкие хуки/плагины передают lifecycle-события через integration_adapter.py
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
- **Тонкие lifecycle-адаптеры**: хуки Claude Code, плагин OpenCode и обёртка Codex нормализуют события через `integration_adapter.py`
- **3-уровневая классификация сессий**: FLUSH_MAJOR (решения/уроки → запускает компиляцию), FLUSH_MINOR (гэтчи → только сохранить), FLUSH_OK (болтовня → пропустить)
- **Non-LLM breadcrumbs** — тегирование промптов и tool-вызовов с ms-латентностью, без API-вызовов
- **Redaction секретов** — API-ключи, токены, длинные base64 вычищаются до любой записи

### Agent-native интерфейс
- **MCP-first доступ** — 12 локальных task-shaped инструментов для recall, контекста, решений, обслуживания, code intelligence и `doctor`
- **Единый response envelope** — каждый инструмент сообщает версию схемы, freshness, качество evidence, warnings и data; MCP resources публикуют health и context
- **Автоматическое здоровье** — SessionStart молчит при норме и инжектирует только degraded/error результаты; `doctor(repair=true)` выполняет лишь безопасные идемпотентные локальные исправления

### Пайплайн компиляции
- **JSON-протокол компиляции** — не требует tool-use агента, работает с любым LLM-бэкендом
- **VERIFY-BEFORE-WRITE** — детерминированная проверка цитат на стороне Python; LLM не может сфабриковать улики
- **Семантический дедуп с quarantine** — update предпочтительнее create; неуверенные или спорные противоречия помещаются в quarantine, а automatic semantic supersession остаётся отключённым
- **Инкрементальность** — SHA-256 хеширование; рекомпилируются только изменённые daily-логи
- **Concurrency-safe** — PID-лок с обнаружением stale; одновременно выполняется только одна компиляция
- **Персистентная очередь задач** — устойчивость к офлайну; отложенные LLM-задачи выполняются на следующей сессии

### Поиск и извлечение
- **Generation-consistent retrieval**: одно проверенное неизменяемое поколение связывает FTS, vectors, graph, tiers и evidence с одним source snapshot
- **Правдивые retrieval traces**: результаты сообщают requested/effective mode, реально использованные signals, generation, состояние reranker и причину fallback
- **Triple-fusion при доступности**: BM25 (FTS5) + Vector (sentence-transformers) + evidence-backed Graph-neighbor RRF
- **Взвешенный RRF**: BM25=2.0, Vector=1.0, Graph=0.5 — предотвращает регрессию на known-item запросах
- **Title + filename boost** — точное совпадение имени файла даёт rank 1 сразу
- **Typed-provenance ранжирование** — `source_authority: user` выше, чем `ai-derived` / `inferred`
- **Темпоральные запросы** — `--as-of YYYY-MM-DD` фильтрует по `valid_to` frontmatter
- **Локальные режимы retrieval** — прямое чтение страниц на малом масштабе, всегда доступный SQLite FTS5 BM25 и опциональный hybrid с vectors/LanceDB + graph + reranker
- **Grounded QA** — извлечённые source spans содержат citation ID, пути, хеши source/span, revision и byte/line ranges; при недостаточных, конфликтующих или не соответствующих времени данных система воздерживается от ответа

### Проактивный интеллект
- **Guardrails** — авто-инжекция выученных корректировок на SessionStart (предотвращает повторение ошибок)
- **Advisory** — поднимает открытые треды, последнее решение, lint-алерты, кросс-проектные инсайты
- **Метакогнитивный контекст** — инвентаризация vault, backlog компиляции, распределение flush-tier
- **Захват обратной связи** — обнаруживает корректировки/предпочтения в транскриптах, сохраняет как кандидаты на промоутер

### Мультипроект и мультиагент
- **Один vault, много проектов** — 5-шаговая collision-safe slug-система, per-project `state.md`
- **Bootstrap проектов** — авто-генерация контекста из git-истории, README, tech-стека
- **Blackboard-протокол** — параллельные агенты клеймят задачи, сигналят завершение, детектят конфликты
- **Loop-детектор** — фиксирует циклические редактирования (fix → review → redo)
- **Agent timeline** — атрибуция: какой агент какое решение принял и когда

### Обслуживание
- **14 lint-проверок (13 структурных + 1 LLM-оцениваемое противоречие)** — битые wikilinks, orphan'ы, missing frontmatter, невалидные supersede-цепочки, temporal validity, gap'ы, sparse pages, missing sources, противоречия
- **Type-aware архивация** — debugging 60 дн, patterns 180 дн, decisions никогда
- **Nightly + weekly расписания** — компиляция, lint, архивация, OKF-миграция (Task Scheduler на Windows, cron на Unix)
- **OKF v0.1 frontmatter** — поля `type`, `confidence`, `source_authority`, `supersede`; авто-миграция с legacy-страниц

### Инфраструктура
- **5 LLM-бэкендов** (авто-детекция): OpenCode → Codex → Claude CLI → OpenAI → Ollama
- **Кросс-платформенность**: Windows, macOS, Linux, WSL2
- **Локально и без daemon-процессов** — установленный baseline включает MCP-пакет; vector search и Cognee остаются опциональными
- **3081 регрессионных тестов**, CI green на Ubuntu + Windows + macOS, Python 3.10 + 3.13
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

> **Примечание для production:** Указанные выше URL ветки `main` могут изменяться. Для production- или аудируемых развёртываний используйте URL конкретного release-тега, например:
> - **macOS / Linux / WSL2:** `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v4.0.0/install.sh`
> - **Windows:** `https://raw.githubusercontent.com/Ekgardt/llm-wiki/v4.0.0/install.ps1`

Установщик:
1. Проверяет требования (Python 3.10+, git)
2. Устанавливает `uv` (быстрый Python-менеджер пакетов), если отсутствует
3. Синхронизирует locked baseline-зависимости (`uv sync --locked --extra mcp-server`)
4. Запускает тестовый набор (3081 тестов)
5. Устанавливает переменную окружения `LLM_WIKI_ROOT` (user scope)
6. Создаёт runtime-директории (`cache/`, `logs/`, `run/`, `cache/cognee/` — gitignored)
7. Регистрирует плановое обслуживание (cron на Unix, Task Scheduler на Windows)
8. Детектирует ваших агентов и подключает их
9. Строит FTS5 search-индекс

### Ручная установка

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync --locked --extra mcp-server
uv run pytest -q          # собирает 3081 тестов
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
| **OpenCode** | MCP + тонкий JS lifecycle-плагин | MCP выполняет чтение/действия; плагин передаёт события в `integration_adapter.py` |
| **Codex CLI** | MCP + тонкая обёртка | MCP выполняет чтение/действия; обёртка передаёт lifecycle-события |
| **Claude Code** | MCP + тонкие settings.json хуки | MCP выполняет чтение/действия; пять хуков передают lifecycle-события |
| **Cursor** | MCP + rules-файл | Настройте MCP; скопируйте `integrations/cursor/rules/llm-wiki.mdc` для инструкций |
| **Antigravity** | MCP + AGENTS.md snippet | Настройте MCP; скопируйте `integrations/antigravity/AGENTS.md` для инструкций |
| **Obsidian** | Опциональный Markdown viewer | Откройте vault напрямую; UI или ingestion-функции Obsidian не требуются |

Все агенты используют общий vault — решение, записанное Cursor, видно OpenCode в следующей сессии.

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
- **RUNTIME** — gitignored. Search-индексы и логи одноразовые; транзакции, состояние очереди и undo-образы в `run/` являются операционным состоянием.
- **Граница авторитетности** — Markdown, Git history и append-only project journals авторитетны. FTS, vectors, базы Evidence Graph, tiers, telemetry и model caches производны и пересоздаваемы.

Полное обоснование дизайна (7 аксиом, диаграмма архитектуры, таксономия памяти, архитектура поиска) — в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Канонический reference структуры (что где живёт, env-контракты, запрещённые layout'ы) — в [docs/STRUCTURE.md](docs/STRUCTURE.md).

---

## Поколения evidence и миграция

`cache/evidence-graph/catalog.sqlite3` выбирает одно неизменяемое активное поколение в `cache/evidence-graph/generations/<generation-id>/`. Candidate регистрируется только после проверки manifest, состава source, хешей artifacts, целостности базы и evidence spans. Активация меняет указатель через compare-and-swap. Сбой или прерывание до активации оставляет предыдущее поколение активным; повреждённое активное поколение пропускается в пользу последнего проверенного предыдущего. Полные orphan generations могут быть зарегистрированы при recovery, но автоматически не активируются.

Удаление `cache/evidence-graph/` удаляет только производное состояние. Сначала остановите активные команды, сохраните `run/` и перестройте cache прежде, чем ожидать generation-backed retrieval. Пока evidence миграции установленных vault отсутствует, сохраняйте legacy `cache/index.sqlite`, `cache/vectors.npy`, `cache/vectors_meta.json` и `cache/lancedb/`. Если проверенное поколение открыть нельзя, retrieval откатывается к этим legacy-путям либо к lexical/live extraction и сообщает fallback. Безопасный rollback никогда не удаляет `knowledge/`, Git history, project journals или `run/`.

Model matrix фиксирует revisions кандидатов и требует EN/RU/ZH quality, resource, license и Pareto gates перед выбором defaults. Новая embedding model или reranker пока не выбраны: **evidence pending**. Существующая опциональная vector-совместимость продолжает использовать закреплённую legacy model. Token counts помечаются как `reported`, `tokenizer`, `estimated`, `mixed` или `unknown`; денежная стоимость отдельно помечается как `reported`, `estimated` или `unknown`. Оценка по UTF-8 bytes предназначена для консервативного планирования и не является независимой от tokenizer гарантией.

Реальное сравнение Graphify и evidence превосходства моделей отсутствуют: **evidence pending**. Детерминированный comparative smoke проверяет только orchestration и не подтверждает claims о качестве или token ratio.

Активация, recovery, rollback, citations и точное поведение MCP описаны в [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

---

## Надёжные операции с памятью

Markdown остаётся авторитетным источником. Runtime SQLite координирует восстанавливаемые записи и очередь, но не является источником знаний. Операционные базы используют rollback-journal, `synchronous=FULL` и no WAL на текущей версии SQLite. State root должен находиться на локальной файловой системе; сетевые пути отклоняются, а обнаружение cloud-синхронизируемых папок выполняется best-effort.

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --repair
uv run python scripts/markdown_transaction.py recover
uv run python scripts/markdown_transaction.py undo <transaction-id>
uv run python scripts/markdown_transaction.py prune --retention-days 30
uv run python scripts/memory_queue.py migrate
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
uv run python scripts/archive_daily.py --commit --hot-days 90
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
```

Доставка очереди выполняется как минимум один раз, поэтому handlers используют стабильные operation ID для идемпотентности. Архив переносит подходящие daily-логи старше 90-дневного hot window в проверенные несжатые BagIt-пакеты и сохраняет логическое разрешение evidence. Неуверенные или спорные для evaluators claims помещаются в quarantine; semantic supersession отключён до прохождения frozen benchmark gate. Процедуры recovery, retention и безопасного удаления описаны в [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

---

## Бенчмарк

> **Историческая legacy-методология**: только BM25/FTS5 по git-tracked публичному корпусу; graph, vectors и reranker отключены. `current-generated-v2` содержал 112 детерминированных запросов: точный заголовок, ключевые слова summary, частичный заголовок и slug. `legacy-60-v1.json` хранит исходные 60 текстов запросов и gold paths дословно, поэтому последующие правки страниц не меняют gate. Ignored личные страницы и `$LLM_WIKI_ROOT` исключены, поэтому clean clone воспроизводит тот же корпус. Это не LoCoMo и не LongMemEval; числа конкурентов получены на других датасетах.

| Историческая метрика | Исторические текущие 112 | Исторические legacy 60 | agentmemory | Zep | Mem0 |
|---------|-------------|-----------|-------------|-----|------|
| Recall@1 | **94.6%** | n/a | n/a | n/a | n/a |
| Recall@3 | **100.0%** | n/a | n/a | n/a | n/a |
| Recall@5 | **100.0%** | **100.0%** | 95.2% | 94.7% | 91.6% |
| Recall@10 | **100.0%** | n/a | n/a | n/a | n/a |
| MRR | **0.9702** | **0.9694** | 0.882 | n/a | n/a |
| Латентность p50 | **6.3мс** | n/a | 14мс | 155мс | 880мс |

Это исторические результаты legacy-runner. Команда по умолчанию теперь запускает frozen retrieval-v2 benchmark. Только отдельный флаг `--legacy-only` выбирает старый gate; сочетание с `--semantic` или `--report` завершается закрытым отказом.

Запустите retrieval-v2: `uv run python benchmark/run_benchmark.py`

Воспроизведите старый gate: `uv run python benchmark/run_benchmark.py --legacy-only`

### MCP agent interface

Локальный stdio MCP-сервер предоставляет **12 task-shaped инструментов**, включая `doctor`, единый response envelope и health/context resources. `find_dead_code(directory)` возвращает консервативные кандидаты, а `get_architecture(directory)` — entry points, routes, hotspots по canonical symbol ID и communities. Анализ файловой системы требует явно заданную существующую директорию, не принимает корень диска и не использует CWD как fallback.

---

## Сравнение

| Возможность | LLM Wiki | agentmemory | ReMe | akitaonrails |
|-------------|----------|-------------|------|--------------|
| Markdown-first | Да | Нет | Да | Да |
| Мультиагент (3+ инструмента) | Да (5) | Да (32+ через MCP) | Только Claude | Да (12+) |
| Поддержка IDE | Cursor + Antigravity; Obsidian как опциональный viewer | Нет | Нет | Нет |
| Compile-not-retrieve | Да | Нет | Нет | Нет |
| VERIFY-BEFORE-WRITE | Да | Нет | Нет | Нет |
| Guardrails (выученные корректировки) | Да | Нет | Нет | Нет |
| Blackboard-координация | Да | Нет | Нет | Нет |
| Loop-детектор | Да | Нет | Нет | Нет |
| Agent timeline | Да | Нет | Нет | Нет |
| Feedback learning | Да | Нет | Нет | Нет |
| Локально / без daemon | Да | Нет (Docker) | Нет (pip) | Нет (Rust) |
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
