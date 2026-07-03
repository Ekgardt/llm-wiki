# LLM Wiki — Память для ИИ-агентов

![CI](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Проактивная система памяти для соло-разработчиков, управляющих несколькими ИИ-агентами. Markdown-first. Ноль облачных затрат. Recall@5 = 100%. $0/мес.**

Языки: [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## Что это

Ваша память — это **markdown-файлы на вашем диске**. Не облако, не векторная БД, не подписка. Обычные `.md` файлы, читаемые в Obsidian, отслеживаемые через `git diff`, принадлежащие только вам.

Система использует подписки на LLM, которые у вас **уже есть** (OpenCode, Codex CLI, Claude Code), для классификации сессий, компиляции знаний и проактивного контекста.

## Установка (одна команда)

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash
```

**Windows:**
```powershell
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

## Бенчмарк

| Метрика | Результат |
|---|---|
| Recall@2 | **100%** |
| Recall@5 | **100%** |
| MRR | **0.952** |
| Latency p50 | **28ms** |
| Стоимость/мес | **$0** |

## Ключевые возможности

- **Тройной гибридный поиск**: BM25 + Vector + Graph-neighbor (взвешенный RRF)
- **Guard rails**: автоматическая инъекция выученных правил в начале сессии
- **Blackboard**: координация параллельных агентов через общий чёрный ящик
- **Loop detector**: предотвращение бесконечных циклов «исправь → проверь → переделай»
- **Agent timeline**: «кто какое решение принял и когда»
- **Feedback learning**: система учится на ваших поправках
- **5 LLM backends**: OpenCode → Codex → Claude → OpenAI → Ollama (автоопределение)
- **Persistent queue**: задачи выживают офлайн, обрабатываются при следующей сессии
- **OKF v0.1**: 100% соответствие открытому формату знаний
- **155 тестов**, CI green на Ubuntu

## Как это работает

```
Работаете в OpenCode / Codex / Claude Code
            ↓
Система записывает действия + классифицирует сессии
            ↓
Фоновый compile превращает сырые логи в страницы знаний
            ↓
Следующая сессия: guard rails + advisory + контекст — автоматически
            ↓
Агент продолжает где остановились — без повторных объяснений
```

## Поддерживаемые агенты

| Агент | Интеграция |
|---|---|
| OpenCode | Plugin (JS, autoload) |
| Codex CLI | PowerShell wrapper |
| Claude Code | Hooks (settings.json) |
| Cursor | Rules file |
| Antigravity | AGENTS.md |

## Архитектура

3 слоя (по Karpathy):
- `raw/` — неизменяемые исходные материалы
- `wiki/` — курируемые знания (третье лицо, цитируемые)
- `memory/` — лор проекта (первое лицо, сессионный)

13 проверок lint, автоматический nightly compile (03:00), еженедельная очистка (воскресенье 04:00).

## Лицензия

[MIT](LICENSE) — делайте что хотите.
