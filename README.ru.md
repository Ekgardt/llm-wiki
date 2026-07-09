# LLM Wiki — Память для ИИ-агентов

**Языки:** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

![CI](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Tests](https://img.shields.io/badge/tests-176%20passing-brightgreen.svg)](https://github.com/Ekgardt/llm-wiki/actions)
[![Benchmark](https://img.shields.io/badge/Recall%405-100%25-blue.svg)](benchmark/run_benchmark.py)

**Проактивная система памяти для соло-разработчиков с несколькими ИИ-агентами. Markdown-first. Ноль облака. Recall@5 = 100%. $0/мес.**

Большинство систем памяти (Mem0, Zep, Letta) тянут данные в своё облако и берут подписку. Эта держит всё **на вашем диске** как обычный markdown — Obsidian, `git diff`, только ваши файлы — и использует LLM, которые у вас **уже есть** (OpenCode, Codex CLI, Claude Code).

```
Работаете в OpenCode / Codex / Claude Code
            ↓
Система тихо пишет breadcrumbs и классифицирует сессии
            ↓
Фоновый compile превращает логи в страницы знаний
            ↓
Следующая сессия: guard rails + advisory + контекст
            ↓
Агент продолжает с места остановки — без повторных объяснений
```

---

## Бенчмарк (июль 2026)

> **Честно про методику:** known-item retrieval по публичным sample-заметкам
> (живой счётчик: `benchmark/run_benchmark.py`). Это **не** LoCoMo / LongMemEval.
> 100% Recall@5 на маленьком curated-наборе; на 500+ страницах ждите 85–95%.
> Цифры конкурентов — с других датасетов, прямое сравнение некорректно.

| Метрика | **LLM Wiki v3.3.1** | agentmemory | Zep | Mem0 |
|---|---|---|---|---|
| **Recall@2** | **100%** | n/a | n/a | n/a |
| **Recall@5** | **100%** | 95.2% | 94.7% | 91.6% |
| **Recall@10** | **100%** | 98.6% | n/a | n/a |
| **MRR** | **0.942** | 0.882 | n/a | n/a |
| **Latency p50** | **41ms** | 14ms | 155ms | 880ms |
| **Токены/поиск** | **0** | ~1900 | $$ | $$ |
| **$/мес** | **$0** | ~$10 | $200+ | $50–150 |

Воспроизведение: `uv run python benchmark/run_benchmark.py --semantic`

---

## Главное

### Пайплайн
- **SKEPTICAL compiler** + VERIFY-BEFORE-WRITE (проверка цитат в Python)
- **3-tier FLUSH** (MAJOR / MINOR / OK)
- **JSON-compile** — любой LLM backend
- **COMPILE_AUDIT** — verified / dedup / stubs / contradictions

### Поиск
- **BM25 + Vector + Graph** (взвешенный RRF)
- Title/filename boost, `--project`, `--since` / `--as-of`
- `source_authority` в ранжировании

### Проактивность
- Guard rails, advisory, metacognitive context
- Feedback capture → promotion в знания

### Мульти-агент
- Blackboard, loop detector, agent timeline

### Инфра
- 5 backend’ов: OpenCode → Codex → Claude → OpenAI → Ollama
- Очередь offline-задач, PID-lock compile
- Nightly/weekly (cron / Task Scheduler)
- OpenCode plugin, Codex wrapper, Claude hooks (+ merge settings)
- Cursor + Antigravity
- **176 pytest**, CI + gitleaks

---

## Установка (одна команда)

**macOS / Linux / WSL2:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

Установщик: зависимости, **176 тестов**, `LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT`, расписание, детекция агентов, Claude merge, индекс поиска.

**Вручную:**
```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
uv sync
uv run pytest -q          # 176 tests
```

### Агенты
| Агент | Как |
|---|---|
| OpenCode | plugin → `~/.config/opencode/plugins/` |
| Codex | wrapper / `codex_memory.py` |
| Claude Code | hooks + `merge_claude_settings.py` |
| Cursor | `integrations/cursor/rules/` |
| Antigravity | `integrations/antigravity/AGENTS.md` |

Опционально semantic search: `uv pip install sentence-transformers`

---

## Архитектура (three-zone)

```
CODE         scripts/  tests/  docs/  skills/  rules/  integrations/
KNOWLEDGE    knowledge/{daily,notes,projects,raw,inbox,feedback}
RUNTIME      $LLM_WIKI_STATE_ROOT/{run,logs,cache}   # вне vault
```

```
Сырые источники / сессии
    ↓ capture
knowledge/daily/  (append-only)
    ↓ compile
knowledge/notes/  (durable pages)
    ↓ search (BM25 + Vector + Graph)
контекст сессии (guard rails + advisory)
    ↓ SessionStart
агент видит правила, решения, open threads
```

---

## Публичная история

Перед open-source история git очищена от личных данных. Число коммитов ≠ объём работы.  
**176 тестов** проверяют код. Sample `knowledge/daily/` — синтетические Evidence, без личных сессий.

---

## Платформы

- **Windows** — полный support (Task Scheduler, PowerShell)
- **macOS/Linux** — Python-скрипты + cron/systemd
- **OpenCode** — везде (JS)
- **Codex wrapper** — PowerShell; на Unix — alias на `codex_memory.py`

---

## Сравнение

| | **LLM Wiki v3.3.1** | agentmemory | ReMe |
|---|---|---|---|
| Markdown-first | ✅ | ❌ | ✅ |
| Multi-tool | ✅ OpenCode+Codex+Claude | MCP | Claude |
| IDE | ✅ Cursor+Antigravity | ❌ | ❌ |
| Guard rails / blackboard / loop | ✅ | ❌ | ❌ |
| $0/мес, zero cloud | ✅ | ✅ | ✅ |
| Recall@5 | **100%** | 95.2% | n/a |

---

## Credits

- [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [Harrison Chase — Wiki Memory](https://blog.langchain.dev/wiki-memory/)
- [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

## Лицензия

[MIT](LICENSE)
