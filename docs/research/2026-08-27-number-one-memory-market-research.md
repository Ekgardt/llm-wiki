# Рынок памяти агентов и код-интеллекта: что нужно превзойти

Дата: 2026-08-27. Цель владельца: лучшая система памяти в мире — превзойти
конкурентов памяти, codebase-memory-mcp и Graphify по всем параметрам
эффективности.

## 1. Рынок памяти агентов (2026)

Лидеры и их числа, заявленные публично:

- **Mem0** — 51k+ звёзд, $24M; заявляет 93.4% LongMemEval и 91.6% LoCoMo при
  <7000 токенов на извлечение. Управляемая drop-in персонализация.
- **Zep / Graphiti** — темпоральный граф знаний с битемпоральными рёбрами
  (`valid_at` / `expired_at` / `invalid_at`); заявляет 94.7% LoCoMo, 90.2%
  LongMemEval; +15 пунктов к Mem0 на темпоральных вопросах (63.8% против
  49.0% на GPT-4o).
- **Letta/MemGPT** — OS-уровневое управление памятью долгоживущих агентов.
- **LangMem** — для стека LangChain/LangGraph. **Cognee** — local-first (мы
  его уже отставили как мост, но как конкурент жив).
- Стандартные стенды: **LoCoMo** (1540 вопросов: single/multi-hop, open-domain,
  temporal), **LongMemEval** (500), **BEAM**, **Memory Agent Bench** (четыре
  компетенции: точное извлечение, обучение на лету, длинный горизонт,
  выборочное забывание; 22 системы × 5 моделей).

Важная оговорка из собственного исследования 2026-08-23: публичные рекорды
систем памяти не пережили аудит (воспроизведения 92.3%→38.4% и 84%→58.4%),
а независимая работа на одиннадцати наборах показала, что известные системы
не обыгрывают стабильно обычный поиск по тому же материалу. Числа выше —
заявки, не факты; наша стратегия остаётся «хранить материал и искать по нему»,
но теперь её нужно доказать на чужих стендах.

## 2. Наука (2025–2026)

- **HippoRAG-2**: граф на записи + Personalized PageRank на запросе; даже он
  даёт лишь 54.0% на single-hop FactConsolidation — консолидация фактов
  остаётся открытой проблемой.
- **A-MEM**: Zettelkasten-граф с LLM-рёбрами и периодической переписью связей
  (у нас — `reflection.py`, не измерен против базы).
- **Битемпоральность** (Zep): каждое ребро несёт срок действия и момент
  опровержения — у нас есть supersession страниц, но не битемпоральные ребра
  утверждений.
- **Детерминированная свежесть** («Don't Ask the LLM to Track Freshness»):
  разрешение конфликтов памяти без LLM — ровно наша линия (детерминированная
  supersession), нужно измерить на их протоколе.
- **Забывание по бюджету** (FiFA): ограниченные политики забывания сохраняют
  связность и приватность — наш 90-дневный архив с восстановлением уже такой
  формы, не измерен.
- **Порядок во времени** (Segment Trees for Long-Horizon Agents), **SSGM**
  (управление рисками эволюции памяти), **Entity-Collision** (протокол
  атрибуции прироста от памяти) — готовые протоколы измерения для нашего
  стенда.

## 3. Код-интеллект

- **Graphify** (Graphify-Labs) — 110k+ звёзд; детерминированный AST-граф без
  векторов, «каждое ребро объяснено»; токен-бюджетные подграфы, NL-запросы,
  git-freshness, имена сообществ через host-LLM sampling, Streamable HTTP MCP,
  28 tree-sitter грамматик локально, PostgreSQL-схемы, документы/PDF в графе.
- **codebase-memory-mcp** — наш текущий инструмент: произвольные графовые
  запросы, сниппеты по квалифицированному имени, мультирепо, фоновое
  自обновление, покрытие по путям, ingest_traces.
- **Sourcegraph Cody** (enterprise, $59/мес) — контекст до 10 репозиториев
  одновременно; планка мультирепо-контекста.

Разрыв llm-wiki против этой тройки измерен сегодня и записан в реестре:
нет query_graph, нет сниппета по символу, одно-репозиторное поколение,
ночная (не непрерывная) свежесть, нет покрытия по путям и трасс.

## 4. Наши уникальные преимущества (что конкуренты не делают)

- Память и код-интеллект в одном продукте с одной провенансной моделью.
- Fail-closed надёжность: транзакции с undo, карантин с родословной, DLP на
  границе, доказательства цитат по байтам — ни один из конкурентов не даёт
  таких гарантий записи.
- Локальность без API-ключей на всём пути, включая семантику.
- Самонаблюдение: доктор, самообновление, самоизмерение стендами.

## 5. Определение победы

«Номер 1» — только парные измерения, по правилу дорожной карты 2026-08-23:
пункт закрыт, когда измерение на настоящих данных показало выигрыш против
базы. Чужие заявки не воспроизводимы — значит, победа фиксируется на
публичных стендах нашим прогоном + на нашем стенде применения.

## Источники

- [Best AI Agent Memory Frameworks in 2026 (Atlan)](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- [State of AI Agent Memory 2026 (Mem0)](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [AI Agent Memory Systems in 2026: Compared (Dev Genius)](https://blog.devgenius.io/ai-agent-memory-systems-in-2026-mem0-zep-hindsight-memvid-and-everything-in-between-compared-96e35b818da8)
- [Graphify-Labs/graphify (GitHub)](https://github.com/Graphify-Labs/graphify)
- [graphify-mcp (GitHub)](https://github.com/yasinyaman/graphify-mcp)
- [Graphify hits 63.2K stars (Augment Code)](https://www.augmentcode.com/learn/graphify-63k-stars-knowledge-graphs)
- [Memory Agent Bench / consolidation problem (Hindsight)](https://hindsight.vectorize.io/blog/2026/05/21/agent-memory-consolidation)
- [Temporal Semantic Memory (arXiv 2601.07468)](https://arxiv.org/pdf/2601.07468)
- [Reliable Post-Retrieval Assembly for Agent Memory (arXiv 2606.01435)](https://arxiv.org/pdf/2606.01435)
  — cited here 2026-08-27 under the wrong title "Deterministic Memory Conflict
  Resolution"; corrected 2026-08-28 after the paper was actually fetched.
- [Segment Trees for Long-Horizon Agents (arXiv 2606.04555)](https://arxiv.org/pdf/2606.04555)
- [SSGM: Governing Evolving Memory (arXiv 2603.11768)](https://arxiv.org/html/2603.11768v1)
- [Entity-Collision attribution protocol (arXiv 2605.29630)](https://arxiv.org/pdf/2605.29630)
- [Sourcegraph Cody Review 2026](https://www.buildfastwithai.com/ai-tools/sourcegraph-cody)
