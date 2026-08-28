# What an answer must say about what it cost

Date: 2026-08-28. Backlog item `OPS-02`
(`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` §12, "Эксплуатация"): *"телеметрия
стоимости ответа (токены/латентность на запрос) в конверте — закон 4 про
экономию токенов становится измеримым."* This is the research that has to exist
before a new module is added to the answer path.

## The gap this closes

`CLAUDE.md` §1 and rule 4 both require the product to spend tokens frugally in
operation. Today that requirement is **asserted, never observed at runtime**.
Every cost number this vault has was produced by a benchmark harness after the
fact:

| number | where it came from | when |
|---|---|---|
| 3.4× codebase-memory-mcp's tokens on 10 paired tasks | `benchmark/run_code_parity.py` (`CODE-07`) | 2026-08-28 |
| mean 333.5 → 274.6 tokens; total 412 070 → 9 321 | `CODE-06` reduction run | 2026-08-28 |
| 4 222 tokens per retrieval | `MEM-10` LongMemEval run | 2026-08-28 |

An operator running the product day to day sees none of these. A law that is
only measurable in a harness is a law nobody can check while the thing is
running.

## What comparable tools report, and under what names

The external anchor is the **OpenTelemetry GenAI semantic conventions**, which
in 2026 moved to their own repository
(`open-telemetry/semantic-conventions-genai`; the old
`opentelemetry.io/docs/specs/semconv/gen-ai/` pages are now redirect notices —
checked today, both the docs site and the `semantic-conventions` mirror return
only the move notice). Read first-hand from
`docs/gen-ai/gen-ai-metrics.md` and `docs/registry/attributes/gen-ai.md` on
`main` today:

| identifier | instrument | unit (UCUM) | requirement |
|---|---|---|---|
| `gen_ai.client.token.usage` | Histogram | `{token}` | Recommended |
| `gen_ai.client.operation.duration` | Histogram | `s` | Recommended |
| `gen_ai.execute_tool.duration` | Histogram | `s` | Recommended |
| `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` | span attribute | int | — |
| `gen_ai.token.type` | attribute of the usage metric | `input`; `output` | **Required** |

Three things in that registry decide the design here.

**1. Tokens and duration are two separate signals, both per operation.** There
is no single "cost" number. `gen_ai.execute_tool.duration` is described as *"The
duration of a single tool execution"* — an MCP tool call is exactly that
boundary, which is why the block is attached where one tool call ends rather
than around the whole server.

**2. Token type is Required, not optional.** The conventions refuse to let a
count float free of what it counts. Our answer is the tool's output, which
becomes the calling agent's *input* tokens; the block therefore names what it
measured (the answer body) rather than emitting a bare number.

**3. The conventions already state the fail-closed rule, in stronger words than
this task asks for.** On `gen_ai.client.token.usage`:

> This metric SHOULD be reported when an operation involves the usage of tokens
> and the count is readily available. […] If instrumentation cannot efficiently
> obtain number of input and/or output tokens, it MAY allow users to enable
> offline token counting. **Otherwise it MUST NOT report usage metric.**

So: an unavailable count is *not reported*, never reported as zero. And
"offline token counting" is the sanctioned category for what we do — a local
estimate rather than a provider-returned count. It must be labelled as such,
which is why `estimate_method` rides beside the number instead of living only
in documentation.

MCP itself offers nothing here: the 2025-03-26 specification defines a `_meta`
field on results for out-of-band metadata but names no cost, size or duration
key, and carries no response size limit — the same gap
`docs/research/2026-08-28-token-budgeted-answers.md` found when it looked for a
truncation signal. Nothing in the protocol tells a caller what an answer cost,
so the answer body has to say it or nothing does.

## Why the placement fails closed

**Placement: `envelope["data"]["answer_cost"]`, not a new top-level envelope
field.** Not a stylistic choice — `mcp_contract.envelope_schema()` declares
`"additionalProperties": False` over the twelve top-level keys and that schema
is handed to the SDK as every tool's `outputSchema`. A thirteenth top-level key
would make every answer fail its own declared schema. `data` is the one branch
the schema leaves open (`"data": {}`), and it is where the vault already parks
`_meta`, `retrieval_trace` and `answer_budget`. Reusing that precedent also
keeps `mcp_contract.py` untouched.

**A missing measurement reads as missing.** Three failure modes, three
behaviours, none of them zero:

| what fails | what the answer says |
|---|---|
| token estimate raises (unserialisable payload) | `tokens_estimated: null` |
| the elapsed clock is unusable (negative / non-numeric) | `duration_ms: null` |
| a stage's freshness is neither fresh/stale nor missing | the stage is listed `unknown:`, never folded into `ran:` |
| the answer's own size is unknown | no block — an answer that cannot be shown to afford it is charged nothing |
| the body is not an object, or the block cannot be built | the key is **absent** — never `{}` and never `0` |

`0` is a legal and meaningful value for both tokens and milliseconds, so it can
never double as "unknown". This is the same rule OTel states above, and the
same rule `answer_budget` follows when it refuses a budget too small rather
than returning a quietly shortened answer.

**Stages are read, not re-derived.** `effective_mode`, `signals_used`,
`fallback_reason` and `reranker_fallback_reason` already exist on
`data.retrieval_trace`, and `mcp_server._recall_components` already folds them
into the envelope's `components` map as `fresh` / `missing` / `unknown`. The
cost block reads that map. A second derivation of "did the dense leg run" that
could disagree with the first would be worse than none — the same argument that
makes this module import `answer_budget.estimate_tokens` instead of writing a
second token counter.

## The estimator, and how wrong it is

`answer_budget.estimate_tokens` is `len(json.dumps(...)) // 4` — explicitly
"not a tokenizer", chosen because a real count needs a network round trip and
an API key, which do not belong on a local offline answer path. It is the same
approximation `benchmark/run_code_parity.py` uses, so a runtime number is
directly comparable to the `CODE-06`/`CODE-07`/`MEM-10` numbers in the table
above. That comparability is the whole reason for reusing it.

It has a known bias, measured on this vault today rather than assumed: the
estimator serialises compactly, while the wire form is `indent=2`.

| tool | estimator (compact) | wire chars ÷ 4 | estimator understates by |
|---|---|---|---|
| `vault_status` | 101 | 111 | 9.0 % |
| `wiki_overview` | 124 | 142 | 12.7 % |
| `recall` (limit 5) | 12 525 | 13 046 | 4.2 % |

The bias shrinks as the answer grows, because indentation is charged per line
and large answers are dominated by long string values. On the answers that cost
anything it is ~4 %. Stated, not corrected: correcting it would mean a second
counter that disagrees with the benchmark harness, which is the one thing this
note refuses to do.

## What the block costs, and why it is conditional

Measured on the live vault today, by the same estimator the block reports with
(`tests/test_answer_cost.py` holds both numbers and a ceiling):

| block | tokens | wire chars at `indent=2` |
|---|---|---|
| plain answer, no optional stages | **23** | 134 |
| retrieval answer, stage line + refusal reason | **51–52** | 257 |

The block was trimmed twice against its own subject before those numbers were
accepted. A `schema_version: "answer-cost/v1"` field cost 10 of the first
draft's 32 plain tokens for a version nothing validates against — the envelope
already versions itself — and three separate stage keys cost 23 tokens against
the packed `stages` line's 14, because at `indent=2` every extra key is a whole
line. Telemetry that reports rule 4 does not get to be exempt from it.

Against the answers that motivate `OPS-02` this is a rounding error. Against a
cheap answer it is not. Measured, per tool:

| tool | answer | block | share |
|---|---|---|---|
| `recall` (lexical-only fallback) | 323 148 | 51 | **0.016 %** |
| `recall` (dense leg ran) | 12 525 | 51 | 0.41 % |
| `get_context` (2 slugs) | 7 942 | 33 | 0.42 % |
| `read_page` | 1 198 | 24 | 2.0 % |
| `vault_status` | 101 | 23 | **22.8 %** |

A block that spends a quarter of a cheap answer describing itself breaks the
very law it exists to make measurable. So the default is conditional and the
condition is the measurement itself: the block is attached when it costs **≤ 1 %
of the answer it describes** (`MAX_SHARE_OF_ANSWER`) — roughly 2 300 tokens and
up for a plain answer, 5 200 and up for one carrying stage fields.
`LLM_WIKI_ANSWER_COST=always` forces it onto every answer for an operator
auditing cheap tools; `=never` turns it off entirely. When the condition
suppresses it the key is absent, which by the rule above reads as *not
measured* — never as free.

Two honest costs of that choice, named rather than buried:

- **A cheap tool's answer does not state its cost by default.** `read_page` at
  1 198 tokens sits just above the line at 2.0 % and carries nothing. The flag
  is the answer for anyone who needs it to.
- **`get_decisions` can never carry the block at all**, on any setting: its
  `data` is a JSON *list*, and a key cannot be attached to a list without
  changing the answer's shape for every existing caller. The block is left
  absent rather than the answer reshaped. It is the only one of the twelve
  tools in this state.

## What it found on the first day

The block earned itself before the tests were finished. Three consecutive
`recall` calls, same query, same `limit: 5`, one process:

| call | tokens | duration | stages |
|---|---|---|---|
| 1 (cold) | **323 148** | 40 251 ms | `ran: lexical; not_run: dense, graph, reranker` |
| 2 (warm) | 12 525 | 22 414 ms | `ran: dense, lexical; not_run: graph, reranker` |
| 3 (warm) | 12 525 | 7 865 ms | `ran: dense, lexical; not_run: graph, reranker` |

The same question costs **25.8× more tokens** when the dense leg does not get
to run, because the lexical leg's pool is returned without the dense leg's
narrowing — a 1.29 MB answer against 50 KB. The cost block names both halves of
that on one line: the size, and `not_run_reason: optional_stage_timeout`.

This is exactly the class of fact `OPS-02` exists to surface, and it was
invisible before: a harness run measures the healthy path, and the operator who
hits the cold path sees only a slow answer, never a 26× bill. It is left
unfixed here — the fallback's pool size lives in `scripts/retrieval.py`, which
this task does not own — and recorded as the first thing the telemetry caught.

## What is deliberately not built

**No durable record.** `CLAUDE.md` §1 puts derived telemetry in the disposable
tier, and `scripts/retrieval_telemetry.py` already owns a private, bounded,
disposable store at `cache/evidence-graph/telemetry.sqlite3` with a byte
ceiling and a row ceiling. Writing per-answer cost rows there would add a
writer, a retention argument and a size ceiling to the hottest path in the
product for a number the answer already carries. If the owner later wants
history, that database is where it goes — never `knowledge/`, never `run/`.

**No cost block on the timeout envelope.** `_timeout_envelope_text()` is
computed once at server construction and reused as a constant, and
`_tool_timeout_envelope_text` rebuilds only the navigation variant; neither has
the call's start time in scope. A timed-out answer therefore carries no cost
block. That is the most expensive case and the one an operator would most want
priced, so it is a real gap — named here, not closed, because closing it means
threading a clock through two call sites in the async handler that this task's
file boundary does not cover cleanly.

**No input-side count.** OTel's `gen_ai.token.type` has two values and this
block reports one of them. The tool's *arguments* are also tokens the agent
paid for, but they were paid before the tool ran and are visible to the caller
already; counting them here would double-count them in any sum.

## Sources

- OpenTelemetry GenAI semantic conventions,
  `open-telemetry/semantic-conventions-genai`, `main`, read 2026-08-28:
  `docs/gen-ai/gen-ai-metrics.md` and `docs/registry/attributes/gen-ai.md`.
- "Inside the LLM Call: GenAI Observability with OpenTelemetry",
  <https://opentelemetry.io/blog/2026/genai-observability/>, read 2026-08-28.
- `docs/research/2026-08-28-token-budgeted-answers.md` — the MCP-specification
  reading (2025-03-26: no size limit, no truncation signal) reused here.
- `scripts/answer_budget.py` — the single estimator, and the precedent for a
  self-describing block that holds back its own allowance.
- `scripts/retrieval_telemetry.py` — the disposable tier a durable record would
  belong to.
