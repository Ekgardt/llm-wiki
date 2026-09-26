# LLM Wiki

[![Tests](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-5.0.0-blue.svg)](CHANGELOG.md)

**Одна локальная память для всех ваших AI-агентов — обычный Markdown на вашем диске, в Git, принадлежит вам.**

Claude Code, Codex и OpenCode забывают всё, когда сессия заканчивается. LLM Wiki
записывает, что происходило в каждой сессии, за ночь сводит это в короткие страницы
знаний и отдаёт следующей сессии — в любом из этих агентов — решения, уроки и
состояние проекта. Больше не нужно объяснять одно и то же дважды.

Хранение, захват, поиск и MCP-сервер работают локально. Чтобы превратить сессии в
страницы, нужна языковая модель: та, что вы указали, или первая найденная среди
OpenCode, Codex, Claude, OpenAI и Ollama. Локальна только Ollama, остальные — облачные
сервисы, поэтому автовыбор не гарантирует, что всё останется на машине. Текущая
версия: **5.0.0**.

**Языки:** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## Как это работает

```
Вы работаете с агентом как обычно
      ↓  тонкие хуки передают каждое событие сессии в integration_adapter.py
Запись сессии   →  knowledge/raw/sessions/<дата>/  (очищенная, хранится для каждой сессии)
Дневной журнал  →  knowledge/daily/<дата>.md
      ↓  компиляция (при старте сессии, если система свободна, и каждую ночь)
Страницы знаний →  knowledge/notes/<slug>.md   (каждая цитата сверена с источником)
      ↓
Следующая сессия в любом агенте: выученные правила, открытые темы, последнее
решение, состояние проекта — и 12 task-shaped MCP-инструментов для вопросов к памяти
```

Идея — «компилировать, а не искать» ([Karpathy, апрель 2026](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)):
вместо поиска по сырым стенограммам в момент вопроса фоновый проход один раз
превращает их в структурированные страницы, и агент читает уже их.

Рабочий день глазами системы:

- Вы чините ошибку в Claude Code и говорите: «больше никогда не используй здесь старый
  API». Сессия записана; ночью поправка становится страницей-правилом.
- Утром вы открываете Codex в том же репозитории. Его первое сообщение уже содержит
  это правило, открытые темы проекта и вчерашнее решение.
- Вы спрашиваете: «почему мы отказались от LanceDB?». Агент вызывает `recall` и
  отвечает страницей решения и точными строками сессии, из которой оно взялось.

---

## Быстрый старт

### Что нужно

- Python 3.10+ и git
- [uv](https://docs.astral.sh/uv/) ровно 0.12.3 — оба установщика отказываются от любой другой версии
- Агент, которым вы уже пользуетесь: Claude Code, Codex или OpenCode
- Node 22 — только для точной навигации по TypeScript и Python (см. ниже)

### Установка

Склонируйте и прочитайте исходники, затем запустите установщик из этой копии:

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
```

**macOS / Linux / WSL2:**
```bash
LLM_WIKI_ROOT="$(pwd)" bash ./install.sh
```

**Windows:**
```powershell
$env:LLM_WIKI_ROOT = (Get-Location).Path
.\install.ps1
```

Установщик собирает собственный `.venv` хранилища по точному lock-файлу, ставит
закреплённые модели поиска, проводит ограниченный по времени production smoke-тест,
подключает каждого найденного агента, регистрирует ночной и недельный проходы и
строит первый поисковый индекс. Для каждого агента он сообщает, что сделал и нужно ли
ещё что-то от вас.

Удалённая установка поддерживается только для точного коммита: задайте
`LLM_WIKI_COMMIT` полным 40-символьным OID коммита и передайте установщик из
доверенного источника. Имена веток и тегов отклоняются. Каждый релиз указывает свой
коммит и SHA-256 каждого файла, который запускает установка:

```bash
uv run python scripts/release_manifest.py v5.0.0 --markdown
```

### Проверка

```bash
uv run python scripts/doctor.py
uv run python scripts/search_memory.py "auth"
```

`doctor` только читает и сообщает, что здорово, что работает в режиме degraded, что
сломано и что запустить.

### Профили зависимостей

MCP входит в production-базу; `mcp-server` остаётся как compatibility alias.
Установщик делает это сам; вручную:

```bash
uv sync --locked --no-default-groups
uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json
```

Команда восстановления по умолчанию только читает. На новом или спокойном хранилище
установщик запускает её с `--apply --adopt-ownership-v3 --confirm-all-agents-stopped`,
чтобы перевести служебные данные на текущий формат баз; если в хранилище уже есть
работа, он сначала просит подтвердить, что ни один агент не запущен. Знания и `run/`
она никогда не удаляет.

Дополнения добавляются к установленному и сохраняют то, что вы уже выбрали:

```bash
uv sync --locked --no-default-groups --inexact --extra hybrid      # векторы + reranker
uv sync --locked --no-default-groups --inexact --extra code-graph  # индекс кода
```

Разработчики ставят группу разработки и запускают полный регрессионный набор:

```bash
uv sync --locked
uv run --locked --no-sync pytest -q
```

Есть pre-commit хуки (ruff, структурный lint, gitleaks).
Опционально; установщик не активирует эти хуки:
`uv run --locked --no-sync pre-commit install --hook-type pre-commit --hook-type pre-push`.

---

## Агенты

| Агент | Статус | Как подключён |
|-------|--------|---------------|
| **Claude Code** | Автоматически, когда слияние настроек проверено | MCP-сервер + хуки в `settings.json`: семь событий жизненного цикла идут в `integration_adapter.py`, два хука добавляют подсказки графа кода к поиску и субагентам |
| **Codex CLI** | Автоматически, когда конфигурация проверена; один раз одобрите хуки в `/hooks` | MCP-сервер + семь хуков жизненного цикла |
| **OpenCode** | Автоматически, когда конфигурация проверена | MCP-сервер + тонкий JS-плагин жизненного цикла |
| **Obsidian** | Только viewer | Obsidian как опциональный viewer: откройте папку хранилища, ставить ничего не нужно |

Cursor и Antigravity сняты с поддержки 2026-08-26: установщик их больше не находит и
не настраивает, а `uninstall` по-прежнему забирает хуки, которые записала прежняя
установка.

Все агенты делят одно хранилище: решение, записанное в Claude Code, есть в следующей
сессии Codex.

### MCP-интерфейс

Локальный MCP-сервер даёт **12 task-shaped инструментов**: `recall`, `read_page`,
`wiki_overview`, `vault_status`, `get_decisions`, `get_context`,
`check_contradiction`, `log_decision`, `compile`, `find_dead_code`,
`get_architecture` и `doctor`. Каждый ответ приходит в едином response envelope с
версией схемы, свежестью, качеством доказательств и предупреждениями, а два MCP
resources отдают здоровье и контекст. Старт сессии молчит, когда всё в порядке, и
вставляет только находки degraded или error. `doctor(repair=true)` ограничивается
безопасными идемпотентными локальными исправлениями.

По умолчанию сервер работает через stdio. Когда одновременно работают несколько
агентов, дешевле один общий локальный сервер:

```bash
uv run python scripts/mcp_http.py --port 8931
```

Он слушает только буквальный loopback, отклоняет любой `Origin` и требует bearer-токен,
который пишет в `<state root>/run/mcp-http/token` с правами 0600.

---

## Что вы получаете

**Захват, который не теряет сессий.** Каждая сессия записывает очищенную копию себя —
разговор, по строке на каждый вызов инструмента, отчёт субагента — до любой оценки её
ценности. Затем классификатор решает, заслуживает ли она ещё и скомпилированной
страницы. Секреты (ключи, токены, пароли в адресах и командах) удаляются до записи.

**Страницам можно доверять.** Компиляция превращает дневные журналы в типизированные
страницы (решение, паттерн, отладка, понятие…) с YAML-заголовком. Python сверяет каждую
цитату со строкой источника и её хэшем до записи страницы; второй проход модели
просматривает каждое изменение и отбрасывает слабые. Противоречие с существующей
страницей записывается, а не затирает её: старая страница помечается `superseded`, а
сомнительные случаи ждут в карантине. Каждая запись — восстанавливаемая транзакция с
отменой в течение двух дней.

**Контекст при старте сессии.** Выученные из ваших поправок правила, открытые темы,
последнее решение, предупреждения lint и находки из других проектов — с учётом
проекта, в котором вы работаете: страница, скомпилированная из сессий одного проекта,
несёт `project:` и не попадает в сессии другого.

**Поиск, который говорит, как он ответил.** Основа — лексический поиск (BM25);
дополнение `hybrid` добавляет многоязычные векторы (`intfloat/multilingual-e5-small`
на ONNX Runtime) и cross-encoder reranker (`BAAI/bge-reranker-v2-m3`), а вопросы о
связях подключают граф доказательств. Ранжирование учитывает, откуда утверждение (от
вас, из веба, от модели, догадка) и на странице какого типа оно стоит. Каждый ответ
сообщает запрошенный режим, реально использованные сигналы и причину отката. Пока
первый индекс не построен, поиск читает Markdown напрямую и говорит об этом.

**Много проектов, одно хранилище.** У каждого репозитория своя папка проекта с
состоянием, контекстом и журналом только на дописывание; новый проект при первой
встрече получает начальный контекст из истории Git и README.

**Обслуживание, которое идёт само.** Ночной проход обновляет код (только fast-forward,
никогда не делает push, отказывается, если обновление заденет изменённый вами файл),
разбирает очередь, компилирует, обновляет поисковый индекс, делает локальный
Git-снимок `knowledge/` и сообщает о здоровье. Недельный проход запускает lint
(17 проверок), архивирует старые дневные журналы и мигрирует заголовки страниц. На
Windows — Task Scheduler, на macOS — LaunchAgent, на Linux — пользовательский таймер
systemd; cron — явный degraded fallback.

**Понимание кода.** `get_architecture` и `find_dead_code` отвечают по индексу кода
ваших репозиториев; точные определения, ссылки, вызывающие и диагностика приходят от
закреплённых языковых серверов (см. [Навигация по коду](#навигация-по-коду)).

---

## Где что лежит

```
CODE        scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE   knowledge/{daily,notes,projects,raw,inbox}
RUNTIME     cache/  logs/  run/        (внутри хранилища, никогда не коммитятся)
```

- **Код** — этот репозиторий.
- **Знания** — ваша память. Репозиторий поставляет её пустой: каждая страница, дневной
  журнал и запись сессии запрещены в `.gitignore`, отслеживаются только README.
  Публикация страницы — отдельное осознанное действие.
- **Служебные данные** не попадают в Git. `cache/` и `logs/` можно удалить и
  пересобрать; `run/` хранит транзакции, очередь и историю отмен и подчиняется правилам
  удаления из [docs/STRUCTURE.md](docs/STRUCTURE.md).
- **Источник истины** — Markdown, история Git и журналы проектов. Поисковые индексы,
  векторы, граф доказательств и телеметрия производны и пересобираются.

Обоснование устройства: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Повседневная
работа, восстановление и резервные копии: [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

---

## Как память защищена

Служебные базы используют rollback journal с `synchronous=FULL`; держите state root на
локальном диске (сетевые пути отклоняются). Очередь доставляет задачи хотя бы один раз,
поэтому каждый обработчик идемпотентен.

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --repair
uv run python scripts/doctor.py --rebuild-generation
uv run python scripts/markdown_transaction.py recover
uv run python scripts/markdown_transaction.py undo <transaction-id>
uv run python scripts/markdown_transaction.py prune --retention-days 30
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
uv run python scripts/archive_daily.py --commit --hot-days 90
```

Дневные журналы старше 90 дней переезжают в проверенные несжатые пакеты BagIt (это
делает недельный проход); доказательства, которые их цитируют, по-прежнему находятся.
Для копии, которая переживёт потерю диска, есть зашифрованная резервная копия Restic
с поэтапным восстановлением; ночной Git-снимок `knowledge/` локален и не зашифрован.
См. раздел о резервных копиях в [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

---

## Навигация по коду

Точные режимы — `definition`, `references`, `implementations`, `type`,
`diagnostics` и позиционные `callers`/`callees` — используют четыре закреплённых
языковых сервера: **Pyright 1.1.411** (Python), **typescript-language-server 6.0.0**
с tsserver 5.9.3 (TypeScript/JavaScript), **gopls v0.23.0** (Go) и
**rust-analyzer 1.98.1** (Rust). Каждый ставится явно; запрос никогда ничего не
скачивает:

```bash
uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile typescript --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile gopls --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile rust-analyzer --state-root "$LLM_WIKI_STATE_ROOT"
```

Файл, который не берёт ни один сервер, получает ответ `unsupported`; если сервера нет
или он сбоит, ответ переходит на индекс кода, а не проваливается.
Путь рассчитан на работу в доверенных локальных репозиториях и не является OS sandbox.
Подробности: [docs/CODE-NAVIGATION.md](docs/CODE-NAVIGATION.md).

---

## Поисковый индекс

`cache/evidence-graph/catalog.sqlite3` выбирает одно неизменяемое активное поколение в
`cache/evidence-graph/generations/<generation-id>/`: полнотекстовый индекс, векторы,
граф доказательств и уровни, собранные из одного снимка ваших страниц. Новое поколение
активируется, только когда его манифест, хэши, базы и участки доказательств прошли
проверку; неудачная сборка оставляет активным предыдущее. Установщик строит первое
поколение, ночной проход обновляет его, а
`uv run python scripts/doctor.py --rebuild-generation` пересобирает по требованию.
Удаление `cache/evidence-graph/` стоит только времени.

---

## Бенчмарк

Поиск проверяется на замороженном публичном синтетическом корпусе
`benchmark/retrieval-v2.json` — многоязычные страницы с градуированными
доказательствами, отвлекающими страницами, историей во времени и вопросами, на которые
нужно отказаться отвечать:

```bash
uv run python benchmark/run_benchmark.py
```

Долгая память измеряется на LongMemEval (`benchmark/run_longmemeval.py`), обработка
противоречий — на своём замороженном корпусе:

```bash
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
```

Сравнения с другими системами памяти здесь не заявлено: их опубликованные числа
получены на других наборах данных.

---

## Участие

Вклад приветствуется. Планка — «выдержит ли это настоящую работу нескольких агентов?».
Настройка, стандарты кода и чеклист релиза (три README, CHANGELOG и версия меняются
вместе) — в [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Благодарности

- [Karpathy's LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — паттерн «компилировать, а не искать»
- [Harrison Chase, "Wiki Memory"](https://blog.langchain.dev/wiki-memory/) — файлы, которые ведёт агент
- [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — нейтральный к поставщику формат знаний в Markdown
- [Anthropic, effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — паттерны захвата, сжатия и субагентов
- [VEP Semantic DNA](https://vep.live) — жизненный цикл уверенности, замены и времени

---

## Лицензия

[MIT](LICENSE)
