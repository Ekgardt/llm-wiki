# An estimate is measured, and an open day waits

Date: 2026-09-27. Audit 2026-09-27 C-6 and the "one token estimator" question.
Files: `scripts/answer_budget.py`, `scripts/answer_cost.py`, `scripts/code_navigation_renderer.py`,
`scripts/maybe_compile.py`,
`scripts/compile_memory.py`, `scripts/integration_adapter.py`, `scripts/memory_state.py`.

## What was measured (primary data, 2026-09-27)

**Bytes per token of this vault's text, from provider-reported usage.** The vault does
not record the usage its own model calls report (`llm_client` keeps `TokenUsage` in
memory only; no own-call transcript with usage survives). The host's session
transcripts do: every request carries `input_tokens + cache_creation_input_tokens +
cache_read_input_tokens`. Between two consecutive requests of one conversation, the
growth of that total, less the previous turn's `output_tokens`, is the token count of
what was appended. Restricted to requests whose only appended content is one tool
result that printed a `.md` file raw (`cat`/`sed -n`/`head`/`tail`, no line numbers),
at least 4 KB:

| text | samples | median bytes/token | lowest |
|---|---|---|---|
| more than 30% Cyrillic | 19 | 3.08 | 2.40 |
| 5–30% Cyrillic | 2 | 2.84 | 2.80 |
| under 5% Cyrillic | 6 | 2.58 | 2.30 |

All tool results of at least 8 KB (365 samples, including code and terminal output):
median 2.42 bytes/token for ASCII, 3.30 for Cyrillic; the lowest (1.24–1.65) are
terminal escape sequences, `ls -l` listings and profiler tables, which no answer or
prompt of the memory carries. The tokenizer is Claude's (Opus 5 / 5.5 sessions); the
compile provider on this vault is Claude as well.

So the answer estimate of 4 bytes per token undercounts Markdown 1.3–1.7x (an answer
asked to fit 2 048 tokens could cost ~3 500), and the prompt-side count of one token per
byte overcounts 2.3–4x.

**Compile padding, from the 77 compile receipts (`knowledge/daily/receipts/`).** Every
receipt records `packing.measured_input_tokens` and its batch manifest. The median batch
is 95% day text. 18 of 77 batches carried less than 8 KB of day text and were filled to
26.4–27.7 KB with context pages (19–26 KB of pages; a 774-byte day went out as a
26 987-byte prompt). The pages are ranked by BM25 overlap with the day; on the live
vault every one of 212 pages shares a word with every recent day (even a 239-byte
file shares one with 186), and a word-overlap floor that drops words present in half
of the pages still keeps 186–212. The score curve has no elbow (top score 17, median
4.5 for the smallest day). No overlap rule separates relevant from filler here.

**Recompiling the open day.** Receipts show the bytes sent per day against the day's
final size: 2026-09-11 sent 36 288 bytes of day text for a 10 663-byte day in 6
batches, 2026-09-12 21 534 for 9 566, 2026-09-24 13 490 for 6 619, 2026-09-23 38 537 for
26 871; closed large days cost only 2–9% extra. A day under 16 KB is one part
(`evidence_resolver.MAX_DAILY_PART_BYTES`), so every append changes that part's digest
and every session start (`integration_adapter._run_session_start_maintenance` →
`maybe_compile.spawn_compile_if_idle`) sends the whole day again, each time padded to
~27 KB: 6 batches ≈ 160 KB of prompt for 10.6 KB of day.

## Sources (fetched 2026-09-27)

1. tiktoken README — <https://github.com/openai/tiktoken>: "On average, in practice,
   each token corresponds to about 4 bytes." That is OpenAI's English-heavy average;
   the measurement above is this vault's text on the provider it uses, and it is lower.
2. Anthropic, Token counting —
   <https://platform.claude.com/docs/en/build-with-claude/token-counting>: "Claude 4.7
   and later models … use a newer tokenizer. The same input text produces approximately
   30 percent more tokens than on earlier models." Consistent with the measured
   2.3–3.1 bytes/token, and a reason the constant must be measured, not quoted.
3. Anthropic, Effective context engineering for AI agents —
   <https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents>:
   "find the smallest possible set of high-signal tokens that maximize the likelihood of
   some desired outcome" and "as the number of tokens in the context window increases,
   the model's ability to accurately recall information from that context decreases".
4. Confluent, Kafka log compaction —
   <https://docs.confluent.io/kafka/design/log_compaction.html>: "all log segments are
   eligible for compaction except for the last segment, meaning the one currently being
   written to. The active segment will not be compacted even if all of its messages are
   older than the minimum compaction time lag."
5. Apache Flink, Windows —
   <https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/operators/windows/>:
   "The `EventTimeTrigger` fires based on the progress of event-time as measured by
   watermarks." A window is evaluated once it is closed; late data causes "late
   firings", not a re-evaluation on every arriving element.

## Decisions

1. **Answers: 2 bytes per token.** `answer_budget.BYTES_PER_TOKEN = 2`, the one estimate
   every answer, cost block and code-navigation budget already imports. Basis: the lowest
   measured Markdown ratio is 2.30, so 2 never undercounted a measured sample, and the
   ~30% tokenizer drift of source 2 stays inside the margin. `answer_cost` names it
   `utf8_bytes/2`. Consequence: a budget now buys about half the bytes it did — the
   answer honours the budget it was asked for. Revisit when the provider's tokenizer
   changes: rerun the measurement (method above).
   - A number the caller states (`budget_tokens`, `token_budget`, its default of 8 192
     and range 256–32 768) and `MAX_BUDGET_TOKENS = 25 000` (Anthropic's documented
     tool-result ceiling, a real-token limit) keep their values and now mean real
     tokens. The 25 000 ceiling was exceeded by up to ~1.7x before; on the dead-code
     fixture 358 of the 461 defensible rows now fit, after every doubtful row went.
   - A bound on something the product itself builds, whose only basis was its byte
     size, keeps that size and is restated in tokens: the rendered navigation answer
     (`MAX_ESTIMATED_TOKENS` 1 200 → 2 400, 4 800 bytes), the budget report's
     allowance (`REPORT_TOKEN_ALLOWANCE` 48 → 96; 48 would no longer cover its
     ~150-byte block), the cost block's ceiling (`BLOCK_TOKEN_CEILING` 60 → 120). The
     cost block's measured size is remeasured: 49 and 108 tokens.
2. **The session start compiles closed days only.** The newest daily log is the one being
   appended to (source 4's active segment); the session-start trigger passes
   `--closed-days-only` and its pending-work check ignores that file. The nightly and a
   manual run still compile every day, so an open day is compiled once a night (source
   5's main firing) and its later entries the next night, instead of on every session
   start. Consequence: what was captured today reaches the compiled pages at the next
   nightly rather than at the next session start; the daily log itself is on disk and
   captured sessions are in `knowledge/raw/sessions/` meanwhile.
3. **Padding is not changed.** Source 3 says fewer, higher-signal tokens; but no overlap
   rule separates the relevant pages on this vault (measured above), and a count or
   score cutoff would be a number without a basis (law 9). Choosing one needs a quality
   measurement — compile runs over real days comparing duplicate-page creation against
   context size — which is a model run the owner has not authorised. Decision 2 removes
   the repeated padded batches of an open day; a day that is small when it closes is
   still padded once.
4. **The prompt side keeps one byte per token, as a byte ceiling.** It is used as a byte
   count in `compile_memory`, `query_memory`, `context_compiler`, `integration_adapter`
   and `mcp_server`, and the compile window of 32 768 has no recorded basis beyond that.
   Moving it to decision 1's estimate would double every compile prompt (from ~27 KB to
   ~55 KB) while decision 3 is open; it is the same measurement that decides both.

## Alternatives rejected

- 4 bytes per token (tiktoken's average): undercounts this vault's Markdown by up to
  1.7x, so answers overrun their budgets.
- The measured median (~2.8): half of all answers would overrun; a budget is a ceiling.
- Recompile only the bytes appended since the last receipt: correct in principle, but
  part bounds are a content-only function shared with the doctor, the archive and
  evidence resolution; making them depend on receipts changes the receipt coverage
  contract of Reliability v3.
- Skip by elapsed time or by a minimum growth since the last compile: an unfounded
  threshold (law 9).

## 2026-10-05: the open-day test uses real receipt authority

The session-start regression fixture still treated `compiled_daily_hashes` as
proof of a processed closed day. That map is now diagnostic; the existing
context-bound receipt selector is authoritative. The original fixture therefore
reproduced `(True, True)` instead of its required `(False, True)`. Returning trust
to the mirror would conceal unprocessed or changed-context source material.

The fixture now uses real Reliability v3 adoption and the canonical active
Markdown coordinator. It publishes a validated empty compile plan for the
heading-only closed day, then reads its strict committed v4 receipt and checks
that it binds the operation that actually committed. The original `(False, True)`
assertion remains intact. A separate adopted-vault control gives the closed day a
matching diagnostic mirror and an empty receipt directory; it must remain pending.
This is a controlled receipt/trigger cycle with no model call, not a semantic
compilation qualification for real user content.

Only the test fixture and this research clarification change. No production
format, path, schema, setting, limit or authority rule changes. Actual navigation
and source inspection covered the trigger, receipt selector, receipt reader,
apply function and adopted coordinator before edits. The original node failed;
the corrected five related test modules passed 62 tests in 12.67 seconds. Lizard
and AST checks accepted all six changed/new fixture callables, including the
nested state mutator; the combined diagnostic maximum was CCN 5, one if and
branch/loop depth two. Ruff passed. Installation remains a separate operation.


## 2026-10-05: repeated parsing belongs to an immutable input

Actual profiling of two adjacent pending native units showed four complete parses
of their shared 15 MB journal. Those parses accounted for about 91 percent of the
instrumented packing time. The verification of permanent heads and parts was a
separate operation and must remain live.

A byte-based packing measurement now owns a pure parsed index for each immutable
original byte object it encounters. Strong references preserve object identity.
Mutable input is parsed afresh, and a new measurement starts a new cache. Every
selected frame still validates its permanent source, linked head, physical bytes
and coverage, including during final batch construction. No filesystem identity
becomes an authority for cached external verification.

Context selection also accumulates the exact serialized byte contribution and
separator cost of each accepted context group. It measures the daily projection
once. Non-additive tokenizer measurements retain their complete measurement path.
The existing source, prompt, receipt, citation and native atomic-unit contracts
remain unchanged. There is no new setting, schema, directory or numerical limit.

The original regressions failed on repeated parsing and repeated daily measurement.
Controls retain rejection of changed original bytes, changed permanent heads,
forged frames and mutable cached input. The matched real two-unit experiment
reduced full parses from four to one; the prompt hashes, measured sizes and all
four permanent-proof checks stayed identical. Instrumented time fell from about
0.346 s to 0.106 s; this is a two-unit result, not a whole-workload speed ratio.

A subsequent read-only full qualification captured approximately 55 MB of actual
inputs as stable immutable per-file snapshots. It completed in 78.89 s, including
8.34 s for capture and 56.29 s for actual batch construction, with a peak RSS of
273684 KiB. The comparison of context selection shared the exact frozen objects
and covered all 38 selected days: chosen contexts matched. The open day changed
between initial inventory and capture; this is not a globally quiescent snapshot
or publication proof. Existing corpus admission was used only in the diagnostic
process; the production compile admission was unchanged.

There were 3268 planned batches, no model requests and no semantic compilation
qualification. Previous whole-workload timings used evolving input, so they do
not establish an exact paired speedup. Automatic same-day consolidation, context
capacity, semantic quality and complete maintenance remain separate work.

Research consulted on 2026-10-05: Python's [functools documentation](https://docs.python.org/3.10/library/functools.html)
and [immutable bytes documentation](https://docs.python.org/3.10/library/stdtypes.html#bytes-objects),
[SQLite snapshot and isolation guarantees](https://www.sqlite.org/isolation.html),
and [Git's explanation of racy filesystem metadata](https://git-scm.com/docs/racy-git).
The chosen scope reuses only pure immutable parsing. A persistent filesystem cache
would add invalidation and lifetime concerns; external prompt caching would not
remove this local work. Neither alternative justifies weakening source verification.
