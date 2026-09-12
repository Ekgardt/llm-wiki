# Three changes to pass them

Dated 2026-09-12. The owner's decision, after seeing the first parity numbers:
all three of the changes below, "это соответствует правилу 2 и 4" — and it does,
which is why each one is measured here before it is written and why two of the
three exist only to spend fewer tokens and fewer seconds.

State when this note was written: after the two defect fixes of `ced8baf`, one
run of the sixteen-task set gives **15 correct for both our surfaces and 15 for
codebase-memory-mcp**, tokens 9 932 / 7 686 against 5 565, wall time 92.8 s /
83.8 s against 35.3 s. Equal on correctness, behind on cost, and still losing
exactly one task in every run.

## Sources

1. `benchmark/code-parity-v3-2026-09-12-run1.json` (this note's starting point)
   and the three `…-v2-…` runs before the fixes.
2. `cProfile` of one `get_architecture mode=query` in a fresh process:
   `_validate_generation` runs three times at 1.10 s each; the second and third
   calls in the same process take 0.30 s and 0.29 s. The artifacts it re-hashes
   are 241 MB `evidence.sqlite3`, 28 MB `search.sqlite3`, 27 MB
   `incremental-manifest.json`.
3. `scripts/evidence_graph.py::EvidenceGraph._admitted_generation` docstring:
   the second validation is the fence against a generation swapped between the
   pointer read and the open, and names its three defences — manifest equality,
   the seal re-check, and the artifact validation.
4. The generation's own node kinds, counted with SQL on the active generation:
   `function` 22 231, `method` 4 534, `class` 1 196, `file`/`module` 711 each,
   `entry-point` 110, plus knowledge kinds. **No constant.** `evidence_graph`
   validates no fixed list of kinds, and each kind carries its own identity
   scheme (`code-entry-point/v1`, and so on), so a new kind is an extractor
   change and not a `graph_schema_version` change.
5. Per-task token table of run 1: the architecture summary costs 2 910 against
   839, and the list answers cost 949 against 56 (T10), 637 against 45 (T01).
   Our rows are JSON objects that repeat their keys; theirs are a header line
   plus one line per row.

## 1. A constant is a node

**Why.** T05 asks where `EDITORIAL_NAMES` is defined. It is the one task the
other tool answers in every run and we answer in none, and the reason is not
retrieval but absence: the extractor emits classes, functions and methods, and a
module-level constant is invisible to the graph. Every "where is this setting
defined" question an operator asks has the same shape.

**What.** `code_extractor` emits a `constant` node for a module-level assignment
whose target is a plain `NAME` in upper case — the convention that separates a
constant from a variable — with the module as its `DEFINES` owner and a
`definition` occurrence carrying the line span. The name lookup surfaces
(`symbol_snippet.SNIPPET_KINDS`, the query mode's start kinds) accept the new
kind. `EXTRACTOR_VERSION` bumps; the graph schema does not change.

**Bound.** Upper-case module-level names only. A lower-case module-level
variable stays out: it is mutable state, the question "where is it defined" is
rarely about it, and every extra node costs bytes in every generation.

## 2. One validation per open, and a seal re-check instead of a second hash

**Why.** 3.3 s of a 4.2 s cold answer is the same immutable generation hashed
three times. Rule 4 asks for fast and frugal; hashing 295 MB twice more to learn
what we already know is neither.

**What.** The first validation already returns a content seal — the per-entry
size, mtime and inode, plus the digests. The second and third points keep their
check but take the cheap form: re-scan the entries, compare the seal, and refuse
exactly as before when it moved. The manifest equality against the catalog row
and the database checks are untouched.

**What is given up, plainly.** A swap that leaves size, mtime and inode
identical inside one call is no longer caught by *this* check. It is still
caught by the manifest-equality check against the catalog and by the database
validation, and the generation directory is immutable after activation by
contract. This is a real reduction in one of three defences, accepted by the
owner on 2026-09-12 with that trade stated.

## 3. Rows carry a header, not repeated keys

**Why.** Tokens 1.78×, and the biggest part of it is the shape of the answer
rather than its content: `{"line": 3267, "qualified_name": "…"}` per row spends
the key names again for every row. The envelope already factors constants out of
rows (`callers_row_constants`), so the mechanism and its precedent exist.

**What.** A row list is emitted as `cols` (the field names, once) plus `rows`
(one array per row), the way the existing `*_row_constants` factoring works, and
the reader's contract says so in the envelope. Applied to the list answers only:
callers, callees, nodes, flows, dependencies.

**Bound.** This changes what every client reads, so it is the one of the three
that needs the owner's word most, and it has it. The envelope keeps its
`schema`/`answer_budget` fields; only row rendering changes.

## Order, and how each is judged

Constants first, because it is the correctness item and the only task we always
lose; then the row shape, which is self-contained; then the validation, which
touches a fence and is therefore written last and with its own tests. After all
three: three runs of both sets, and the decision rule of
`docs/research/2026-09-12-when-we-would-drop-the-other-tool.md` applied to the
result — unchanged, because a rule rewritten after the numbers is not a rule.

Files: `scripts/code_extractor.py`, `scripts/symbol_snippet.py`,
`scripts/graph_query.py`, `scripts/mcp_server.py`, `scripts/code_graph.py`,
`scripts/generation_catalog.py`, `scripts/evidence_graph.py`,
`docs/research/2026-09-12-three-changes-to-pass-them.md`.
