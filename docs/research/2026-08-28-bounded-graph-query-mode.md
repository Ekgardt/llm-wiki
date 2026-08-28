# Многошаговый запрос по графу: ограниченный конвейер шагов вместо Cypher

Дата: 2026-08-28. Пункт `CODE-01` единого бэклога — паритет с `query_graph`
у codebase-memory-mcp: произвольные многошаговые вопросы к графу кода
(«кто зовёт тех, кто зовёт X», «символы в модуле Y вида function»).

## Что дают конкуренты

- **codebase-memory-mcp `query_graph`** принимает сырой Cypher с потолком в
  100k строк; его собственная инструкция советует «for broad queries add
  LIMIT in the Cypher itself» — то есть ограничение стоимости отдано
  вызывающему (инструкции codebase-memory-mcp этой машины, раздел
  MCP-инструкций сессии).
- **Graphify** отдаёт NL-запросы и токен-бюджетные подграфы, не сырой язык
  запросов ([Graphify-Labs/graphify](https://github.com/Graphify-Labs/graphify),
  обзор в `docs/research/2026-08-27-number-one-memory-market-research.md`).

## Почему не Cypher (и не свой язык запросов)

1. **Поверхность инъекции.** Строка запроса, собранная из пользовательского
   текста, — это исполняемый план; Neo4j прямо пишет, что параметризация
   защищает только литералы, а «not everything in a query is
   parameterizable» ([Protecting against Cypher Injection, Neo4j KB](https://neo4j.com/developer/kb/protecting-against-cypher-injection/)).
   Свежий прецедент ровно нашей формы — LLM-инструмент, пробрасывающий
   сгенерированный Cypher в базу: [CVE-2024-7042, LangChain
   GraphCypherQAChain](https://www.sentinelone.com/vulnerability-database/cve-2024-7042/).
   OWASP включил Cypher-инъекции в ASVS
   ([OWASP/ASVS#1824](https://github.com/OWASP/ASVS/issues/1824)).
2. **Неограниченная стоимость.** Произвольный обход графа не имеет верхней
   границы работы; практика ограниченных графовых API — считать стоимость до
   исполнения и отказывать сверх порога, как GitHub GraphQL с его node
   limits и rate-limit score
   ([GraphQL resource limitations, GitHub Docs](https://docs.github.com/en/graphql/overview/resource-limitations)).
3. **Наш движок уже отказывает честно.** `EvidenceGraph.find_nodes` /
   `_execute` поднимают `ValueError("query row ceiling exceeded")`, когда
   строк больше потолка — молчаливого усечения нет по замыслу (измерено
   2026-08-28 на `CODE-05`). Язык запросов поверх этого обязан превращать
   каждый потолок в именованный ответ, а не в голое исключение.

## Решение

Режим `query` инструмента `get_architecture` (12-инструментная поверхность
не растёт — тот же приём, что `provenance`/`snippet`/`coverage` от
2026-08-28). Новый модуль `scripts/graph_query.py`; запрос — закрытая
структурная форма JSON, не текст на языке запросов:

```json
{"start": {"name": "X", "path": "…", "kind": "function"},
 "hops": [{"edge": "calls", "direction": "in"}],
 "limit": 50}
```

- `start` — хотя бы одно из `name`/`path`/`kind`; значения уходят в
  `find_nodes` связанными параметрами, конкатенации SQL нет вовсе.
- `hops` — не больше 3; ребро из закрытого словаря
  (`calls`, `defines`, `imports`, `contains`, `inherits`, `links`,
  `exposes` — измеренный набор рёбер живого поколения), направление
  `in`/`out`. Неизвестное ребро — именованный отказ со списком
  обслуживаемых, не симуляция.
- `limit` — 1..200; фронтир между шагами ограничен тем же числом, усечение
  названо в ответе (`frontier_truncated`).
- Каждый шаг проверяет срок; каждое расширение идёт через
  `EvidenceGraph.neighbors` с его собственными потолками строк и работы.
  Узел, чьё расширение движок отверг потолком, попадает в
  `refused_expansions` поимённо — ответ остаётся ограниченным и честным.

Чего нет намеренно: NL-запросов (отдельная работа; структурная форма — их
целевой план исполнения), агрегаций, фильтров по свойствам внутри шага,
отрицаний («не вызывается никем» — это существующий `find_dead_code`).

## Источники

- [Protecting against Cypher Injection — Neo4j Knowledge Base](https://neo4j.com/developer/kb/protecting-against-cypher-injection/)
- [CVE-2024-7042 — LangChain GraphCypherQAChain injection](https://www.sentinelone.com/vulnerability-database/cve-2024-7042/)
- [Inclusion of Cypher Injection Prevention in ASVS — OWASP/ASVS#1824](https://github.com/OWASP/ASVS/issues/1824)
- [GraphQL resource limitations — GitHub Docs](https://docs.github.com/en/graphql/overview/resource-limitations)
- [Graphify-Labs/graphify (GitHub)](https://github.com/Graphify-Labs/graphify)
- Поверхность cbm `query_graph`: инструкции codebase-memory-mcp этой машины.
- Разрыв: `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, `CODE-01`.
