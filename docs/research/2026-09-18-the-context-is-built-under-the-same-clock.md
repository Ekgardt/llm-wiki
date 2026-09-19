# The context is built under the same clock, and what it drops is said

Dated 2026-09-18 (the audit's lows R-L8 to R-L11, worked the night of 2026-09-17). Four small
defects of the answer path: building the context ignores the answer's deadline, the budget's
names say tokens where the values are bytes and what it sheds is recorded nowhere, five
best-effort failures are swallowed in silence, and the first turn of an entry is never pruned.

Files: `scripts/query_memory.py`, `scripts/evidence_pruning.py`, `scripts/retrieval.py`,
`scripts/access_tracking.py`, `benchmark/longmemeval_vault.py`,
`tests/test_the_context_is_built_under_the_same_clock.py`

## What was found

- **L8, no deadline inside context building.** `_AnswerPass.run` checks the deadline once and
  then calls `build_grounded_context`, which takes none. Inside it `_fitted_selection` retries
  the whole compilation after each single shed, and each retry calls `_narrowed_snapshot`,
  which walks every chunk and every source of the snapshot again. `compile_context` accepts a
  `deadline` and `_compiled_context` does not pass it. So a question whose best pages are long
  can spend an unbounded stretch of the MCP budget inside a loop that nothing can stop, and
  the deadline is noticed only when the generation starts.
- **L9, units and a silent shed.** `ContextBudget(None, 65_536, 1_200, 512)` puts a byte count
  in `max_input_tokens` and subtracts a token count from it. It is consistent with the
  documented "one token per UTF-8 byte" estimate and errs on the safe side, but the field
  names say tokens, the QA constants say bytes in one place and tokens in another, and
  `packed_tokens` is a byte count. Separately, `_packed_context` pops evidence off the tail
  until the rendering fits and tells nobody — the one thing the compile trace exists to say.
- **L10, swallowed failures.** `retrieval._standing_disposition`, `_co_activation_table` and
  `_record_impressions`, and `access_tracking.record_access`, catch everything and continue
  with no record. The repository already decided the opposite for capture: `mcp_server`
  writes a dropped telemetry event to the bounded failure trail with
  `capture_diagnostics.record_capture_failure` ("counted, never silent", audit OPS-21), and
  `access_tracking._note_flush_failure` names a page it could not export.
- **L11, the first turn is never pruned.** `evidence_pruning.prunes` asks whether the chunk's
  bytes *start* with a turn marker. The first chunk of an entry starts with the entry's
  heading, so a first turn of any length is delivered whole — and on this corpus the first
  turn is often the user's longest message. A second edge: `_within_budget` stops as soon as
  `KEEP_BYTES` is spent, and the first sentence is added before any scored one, so a first
  sentence of 600 bytes or more delivers nothing the question actually chose.

## Practice on this date

- A deadline is only a deadline if every stage inside it can see it: the rule this repository
  already follows in `retrieve`, `_executed_plan` and the optional stages, and the reason
  `compile_context` has a `deadline` parameter at all. It is the same rule deadline propagation
  states generally: a caller passes down what it has left rather than the whole of it, and every
  stage below honours it (https://sre.google/sre-book/addressing-cascading-failures/). Passing it costs nothing and is the
  difference between a bounded answer and one that is late for a reason no trace records.
- Best-effort work that fails silently is indistinguishable from best-effort work that never
  ran. The repository's own answer — a bounded, trimmed failure trail with a redacted
  description — already exists and is what doctor reads.
- An extractive compressor keeps the sentences that bear on the query and the speaker marker
  that says who is talking (RECOMP, Xu, Shi and Choi, arXiv:2310.04408, https://arxiv.org/abs/2310.04408 — the shape
  `evidence_pruning` already cites). Nothing in
  that design says the first turn of a file is exempt; the exemption here was an accident of
  testing the start of the chunk rather than the start of the turn.

## The decision

- `build_grounded_context` takes an optional `deadline` and hands it to `compile_context`;
  `_fitted_selection` checks it before each retry, so a shedding loop stops at the same
  instant as everything else. `_AnswerPass.run` passes its own. The snapshot is narrowed
  through one index of chunks by page, built once per call instead of once per retry.
- The QA budget is built by one helper that names its unit — bytes, under the documented
  one-byte-per-token estimate — so the two QA constants stop disagreeing about what they are.
  `_packed_context` reports what it shed; `GroundedContext` carries the count as
  `shed_for_budget`, and the stand records it beside the compiler's own dropped count, so the
  number has a reader.
- The four swallowed failures are named through the mechanism already in place,
  `capture_diagnostics.record_capture_failure`, each with its own kind. Behaviour is
  unchanged: the search still answers, the telemetry is still best effort.
- `prunes` looks for a turn marker at the start of any line of the chunk, so the first turn of
  an entry is pruned like every other; the heading is inside the first sentence, which is
  always kept. `_within_budget` keeps the first sentence and at least one sentence the
  question scored, however long the first one is.
