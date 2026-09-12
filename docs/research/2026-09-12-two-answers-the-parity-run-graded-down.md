# Two answers the parity run graded down

Dated 2026-09-12, after the first honest parity numbers
(`docs/research/2026-09-12-the-first-honest-parity-numbers.md`). The owner's
verdict on those numbers: we were supposed to surpass the other tool, not trail
it by two answers. This note is the part of that gap which is our defect rather
than our design, and it is fixed here. The rest — what costs tokens and what
costs seconds — is measured below and needs decisions that are not mine.

## Sources

1. `benchmark/code-parity-v2-2026-09-12-run1.json`, tasks T04 and T14: the two
   tasks where our answer was graded `partial` in all three runs.
2. The answer our side actually returned for T14:
   `"bindings": "lexical_hits->lexical,dense_hits->Any]] | None"`. The second
   pair names a slice of a type annotation as a parameter.
3. `scripts/code_extractor.py:413` `_parameter_names`: it takes the callee's
   signature string and splits it on `,`. `_graph_seeds(lexical: Sequence[...],
   dense: list[dict[str, Any]] | None)` therefore splits inside
   `dict[str, Any]`, and every parameter after the first comma-carrying
   annotation is misnamed and misaligned.
4. The answer our side returned for T04 (`mode=symbol`): callers and callees
   with their call-site lines, and no line for the definition the question asked
   about. `cbm` answered `scripts/retrieval.py 3030-3050`.
5. The generation already holds that line: in
   `cache/evidence-graph/generations/…/evidence.sqlite3`, the `occurrence` row
   for `_page_diverse` is `role='definition', line_start=3030, line_end=3050`,
   and `scripts/symbol_snippet.py:127` already reads exactly that row.
6. `cProfile` of one `get_architecture mode=query` call in a fresh process:
   4.22 s total, of which 3.92 s is `_active_evidence_graph` →
   `open_active_for_repository`, and inside it `_validate_generation` runs
   **three times** at 1.10 s each. The second and third calls in the same
   process cost 0.30 s and 0.29 s.

## Facts, separated from what follows from them

- T14 is a defect of the extractor, not of the benchmark: any callee with a
  comma inside a parameter annotation gets wrong bindings. It is a class, not an
  instance — the run happened to catch one member.
- T04 is a gap in the answer, not in the evidence: the definition line is in the
  generation and the symbol view does not carry it.
- The 3× validation is what makes our p95 3.0× theirs. Each validation hashes
  the generation's artifacts, which here are 240 MB of `evidence.sqlite3` plus
  28 MB of `search.sqlite3` and a 27 MB incremental manifest. Warm, our answer
  is 0.29 s against their ~2.0 s; cold, 4.2 s against ~2.0 s. The benchmark
  starts a fresh process per call, so it always measures the cold number.
- `evidence_graph.EvidenceGraph._admitted_generation` documents the second
  validation as deliberate: it is how a generation swapped between the pointer
  read and the open is refused. Removing or weakening it is a change to a
  safety fence, so it is not made here.

## What is changed here

1. `_parameter_names` splits the signature at **top-level** commas only —
   depth-aware over `[`, `(`, `{` — so an annotation cannot invent a parameter.
   This changes extractor output, so `EXTRACTOR_VERSION` goes to
   `code-extractor/v13` and the next generation build carries the fix; the graph
   is derived and regenerable, which is exactly what the contract says it is.
2. The symbol view answers where the symbol is defined: `definition` rows with
   the path and the `line_start`/`line_end` the generation already stores, read
   through the reader `symbol_snippet` already has rather than a second lookup.
   The source text is not included — the question is where, and the snippet mode
   exists for what.

Neither change touches the response envelope, a runtime path, or a fence.

## What is measured and left for the owner

- **Tokens, 1.77×.** The biggest single cost is the architecture summary (2 910
  against 839) and the list answers (T10 949 against 56). Our rows are JSON
  objects repeating their keys; the `*_row_constants` factoring already in the
  envelope shows the shape of the answer — a header plus rows would cut it
  again. That is a change to the uniform response envelope every client reads,
  so it needs the owner's word.
- **Seconds, 3.0× p95.** Validating one immutable generation three times per
  answer is the whole gap. Validating once per open and re-checking the cheap
  entry seal instead of re-hashing 295 MB would make the cold call ~1.5 s and
  the warm call unchanged — but it trades a full re-hash for a size/mtime/inode
  comparison inside one process, and that is the torn-generation fence. The
  owner decides whether that trade is acceptable.
- **T05, the constant.** `EDITORIAL_NAMES` has no node in the graph because the
  extractor indexes functions, methods and classes, not module-level
  constants. Adding a node kind is a graph-schema change.

Files: `scripts/code_extractor.py`, `scripts/symbol_snippet.py`,
`scripts/mcp_server.py`,
`docs/research/2026-09-12-two-answers-the-parity-run-graded-down.md`.
