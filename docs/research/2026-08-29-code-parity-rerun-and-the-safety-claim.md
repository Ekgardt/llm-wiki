# Code parity, re-run: what moved, and why the safety claim stays withdrawn

Date: 2026-08-29. Backlog item `CODE-07`
(`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` §12). This re-runs the paired
stand built on 2026-08-28
(`docs/research/2026-08-28-code-parity-first-pairing.md`) after seven changes
landed between the two runs, and settles the safety claim that was withdrawn
the same day it was made (`385f276`).

The first run's own conclusion was: *the evidence bar for `CODE-07` is met,
but it is too early to remove cbm.* That sentence is re-tested at the end.

## How to repeat this

```bash
# a full run, all three columns
uv run python benchmark/run_code_parity.py \
    --out benchmark/code-parity-rerun-2026-08-29-run1.json

# one side only, for cheap repeats
uv run python benchmark/run_code_parity.py --sides cbm \
    --out benchmark/code-parity-rerun-2026-08-29-cbm4.json
uv run python benchmark/run_code_parity.py --sides llm_wiki llm_wiki_best \
    --out benchmark/code-parity-rerun-2026-08-29-llm4.json

# the spread across runs
uv run python benchmark/aggregate_code_parity.py \
    benchmark/code-parity-rerun-2026-08-29-*.json
```

Nothing in `benchmark/run_code_parity.py` or `benchmark/code-parity-v1.json`
was edited. The stand ran exactly as it stands, so the numbers below are
comparable to 2026-08-28 task for task.
`benchmark/aggregate_code_parity.py` and
`tests/test_code_parity_aggregate.py` are new and only read reports.

## The table, with n and the spread

Three full runs (all columns) plus five cbm-only and two llm-wiki-only
repeats: **n=5 for each llm-wiki column, n=8 for cbm.** Ranges are min–max
over runs, not means.

| side | n | correct /13 | wrong | tokens | wall seconds | wrong-but-confident | operator-attention events |
|---|---|---|---|---|---|---|---|
| `llm_wiki` — the surfaces the stand calls | 5 | **8** (no spread) | 5 | **87 527** (no spread) | 85.7–138.1 | 4 | 1 |
| `llm_wiki_best` — the surface that serves the question | 5 | **11** (no spread) | 2 | **36 382** (no spread) | 64.8–120.3 | 2 | 0 |
| `cbm` — codebase-memory-mcp | 8 | **10–11** | 2–3 | 2 406–2 442 | 35.4–69.3 | 2–3 | 0 |

Against 2026-08-28 (`llm_wiki` 0/13, `llm_wiki_best` 10/13, `cbm` 10/13):
llm-wiki's as-configured column moved 0 → 8 and its best column 10 → 11.
`NEW-116`, `NEW-124`, `NEW-135` and the community anchor are visible in that
column and they are real.

**Variance, both sides, stated separately because it is not symmetric.**

* **llm-wiki did not move at all.** Five runs, identical grades on all 13
  tasks and identical token totals to the byte (87 527 and 36 382). The
  active generation `generation-18d015d5499fc78e-7d4cc1d8` was the same for
  every run, and an activated generation is immutable by design.
* **cbm moved in half its runs.** Correct counts across eight runs:
  11, 10, 11, 10, 11, 10, 10, 11 — **four and four**. One task does all of
  it: `T12` (`get_architecture`, the architecture summary) graded `correct`
  in four runs and `wrong` in four, because its answer names `mcp_server`
  only sometimes. `T05` and `T13` were wrong in all eight. cbm's token total
  drifted 2 406–2 442 (1.5 %) with no source change.
* The cause is the same one the first run named, and it reproduced live: the
  cbm graph grew from **28 828 nodes / 154 829 edges** at the start of this
  session to **29 240 / 155 707** about fifty minutes later, with no commit
  to the tools under test. cbm watches and re-indexes in the background.

**Wall time is not a product measurement here and should not be read as
one.** The three full runs started at load average 3.66, 6.45 and 8.23 on a
4 vCPU box shared with other agents, and every column's total scales with
that, not with anything in the code. What is separable:

* cbm CLI floor, measured three times this session: **2.03, 2.04, 2.65 s**.
  The daemon is already resident; the CLI that talks to it is not.
* llm-wiki resident, all stand calls inside one process: import `mcp_server`
  **0.45 s** once, then **1.79 s mean / 1.62 s median** per call for the 16
  `llm_wiki_best` calls and **3.15 s mean** for the 14 `llm_wiki` calls
  (which include two `find_dead_code` and one `summary`).

## Safety: the claim stays withdrawn

The claim under test is the one recorded on 2026-08-28 and repeated in the
register: *silently wrong answers — llm-wiki 0, cbm 3.* It was withdrawn the
same day because who-calls answered `0` for methods (`NEW-124`).

`NEW-124` is genuinely fixed. Asked who calls the method
`recover_expired_leases`, the product now answers `callers: []` **and**
`unresolved_callers` naming five sites with file, line, calling function,
call text and `reason: dynamic_dispatch`, plus an exact
`unresolved_caller_count: 5` and a truncation flag. A caller can tell. That
specific silent zero is gone.

**The claim is still not re-earnable, because hunting turned up two more, and
one of them is worse.**

### Silent-wrong 1 — `mode=dependencies` answers `[]` for everything

`get_architecture mode=dependencies` returns `dependencies: []` for every
input I could construct. Not sometimes: always.

* `scripts/page_status.py` with `reverse: true` → `[]`. Six modules import
  it; the active generation holds **11 `IMPORTS` assertions targeting that
  file**.
* `scripts/retrieval.py` → `[]`. It imports `provenance` at line 16.
* A correct opaque node id for `page_status`
  (`code:node:ae608c5b0aabed5e2b3d6709b6b7e13e`) → `[]`.

The cause is not the row ceiling and not a lookup miss. `find_dependencies`
(`scripts/code_graph.py:2498`) calls `EvidenceGraph.dependencies`
(`scripts/evidence_graph.py:4062`), which walks the generation's
`dependency` table. **That table holds 0 rows** — in the active generation
and in each of the four generations before it, including
`generation-18cfd903a7a4e112-3ce112cb`, the one the 2026-08-28 run measured.
The same file holds 3 934 `IMPORTS` assertions, which is why the `query`
mode answers `T08`/`T09` correctly. This is `NEW-124`'s shape one layer up:
*the graph knew; the answer did not look.*

The only thing shipped alongside the empty list is
`graph_complete: false, unresolved_count: 79263` — and **that pair is
byte-identical on the answers that graded correct**: `T01`, `T02`, `T03`,
`T06`, `T07`, `T10`, `T11` all carry it too. A field that is constant across
right and wrong answers cannot tell a caller which one they are holding. By
the working definition — *a silently wrong answer is one a caller cannot tell
is wrong from the answer itself* — `T08` and `T09` are silent-wrong, not the
flagged-wrong the first run graded them.

### Silent-wrong 2 — `find_dead_code` calls live code defensibly dead

`NEW-135` split the dead-code answer into three outcomes and declared the
survivor defensible: a name that no call text mentions stays
`zero_confirmed_incoming_calls`, "and now it is defensible."

On this repository it is not. A live call of `find_dead_code` returns 873
candidates, 461 of them in that class (435 distinct names). Parsing every
`.py` file under `scripts/` and `benchmark/` with `ast` — so comments and
string literals cannot contribute — and looking for the name loaded as a
value:

| | names | share |
|---|---|---|
| loaded as a value, never called, in `scripts/` or `benchmark/` | **354** | **81.4 %** |
| appear as an actual call | 9 | 2.1 % |
| never referenced at all | 72 | 16.6 % |

The shape is always the same, and it is the shape this repository is built
out of, because rule 5 requires dispatch registries instead of `if`-chains:

* `scripts/doctor.py:6391` — `threading.Thread(target=self._heartbeat_loop)`
* `scripts/blackboard.py:1130` — `parser.set_defaults(handler=_cli_claim)`
* `scripts/memory_queue.py:15572` — `{"restore": _cli_restore}`
* `scripts/evidence_graph_builder.py:1680-1684` — `_plain_row_record`, four
  registry entries
* `scripts/contradiction_pipeline.py:435` — `{"supersede": _superseding_outcome}`

The sharpest instance is self-refuting inside a single run.
`_architecture_dependencies`, `_architecture_symbol` and
`_architecture_path` are all three listed as
`zero_confirmed_incoming_calls`. In the file today they live at
`scripts/mcp_server.py:1535`, `:1579` and `:1547`; they sit in the
`_ARCHITECTURE_MODE_QUERIES` registry (`:1595-1601`) and at the default
branch of `_ARCHITECTURE_MODE_QUERIES.get(mode, _architecture_symbol)`
(`:1632`) — and **the stand executed two of them in the same run that
reported them dead** (`T08`/`T09` call `mode=dependencies`; `T04` calls
`mode=symbol`).

One honest detail about those citations: the answer reports lines 1525,
1569 and 1537, ten lines above where the file has them now. The generation
is a snapshot, `scripts/mcp_server.py` was last rebuilt into it before a
later edit, and the answer names the generation it came from. The line drift
does not touch the finding — the names and the registry are the same — but a
reader following a citation from this tool should expect a snapshot, not the
working tree.

Nothing in the answer marks these differently from the 72 that really are
unreferenced. `graph_complete: false` is present, and is present on the
correct answers too.

What `find_dead_code` does do well, and it should be said: the answer is
bounded and says what it dropped — `candidate_count: 873`, `candidates_by_reason`
with exact totals, and `answer_budget` naming `rows_omitted: 354`,
`omitted_fields`, `truncated`. The listing is honest about its own size. It
is the class label that is wrong.

### Silent-wrong 3, weaker — `T05`, both llm-wiki columns

Asked where the constant `EDITORIAL_NAMES` is defined, `llm_wiki_best`
answers `nodes: []` with `frontier_truncated: false` and
`refused_expansions: []` — a positive assertion that nothing was cut. The
constant exists at `scripts/vault_editorial.py:43`; neither graph indexes
module-level constants. The caveat is about truncation, never about corpus
scope. The first run named this gap and graded it flagged-wrong; under the
strict definition it is a caller who cannot tell, and I grade it silent.
cbm fails the same task the same way, so it does not move the comparison.

### Hand-graded safety, this run

| side | refused | flagged-wrong | silent-wrong |
|---|---|---|---|
| `llm_wiki` | 1 (`T04`) | 1 (`T13`) | **3** (`T05`, `T08`, `T09`) |
| `llm_wiki_best` | 0 | 1 (`T13`) | **1** (`T05`) |
| `cbm` | 0 | 0 | **2–3** (`T05`, `T13`, and `T12` in 4 of 8 runs) |

`T13` is a genuine improvement worth naming: where llm-wiki used to refuse on
the ceiling, it now lists 30 communities and says
`community_count: 4537, community_limit: 30, communities_truncated: true`.
That is a wrong answer that admits what it is — the flagged-wrong grade,
earned.

**Verdict: the safety claim stays withdrawn.** Not because the old
counter-example survived — it did not — but because two larger ones are live
today, one of which (`mode=dependencies`) has never been able to answer on
any generation this vault has published, and the other of which
(`find_dead_code`) mislabels 81 % of its own defensible class on this
repository. The narrow statement that remains true is much smaller than the
one withdrawn: *where llm-wiki answers a who-calls or a reachability question
through the `query` mode or `mode=callers`, it has not been caught answering
wrongly without saying so.* That is not a safety advantage over cbm; on this
stand the hand-graded silent-wrong counts are 1 (`llm_wiki_best`) against
2–3 (cbm), and the difference is one flaky cbm task.

## Tokens: 3.4× became 2.75× on the same questions, and 12.4× overall

Both numbers are true and they answer different questions. Reporting only one
would mislead.

**Same ten tasks as 2026-08-28** — the ten `llm_wiki_best` and cbm both
answered correctly that day (`T01`–`T04`, `T06`–`T11`):

| | 2026-08-28 | 2026-08-29 |
|---|---|---|
| `llm_wiki_best` | 3 335 tok (333.5 mean) | **2 811 tok (281.1 mean)** |
| `cbm` | 984 tok (98.4 mean) | 1 024 tok (102.4 mean) |
| ratio | **3.39×** | **2.75×** |

**All the tasks both sides got right this run** (11 of 13, in the runs where
cbm's `T12` landed): `llm_wiki_best` 22 837 vs cbm 1 837 — **12.4×**. In the
run where cbm's `T12` graded wrong the shared set drops back to ten and the
ratio is 2.75×.

So the whole gap between 2.75× and 12.4× is two tasks, and both are tasks
llm-wiki used to *refuse*:

| task | llm-wiki tokens | cbm tokens |
|---|---|---|
| `T12` architecture summary | **20 026** | 813–818 |
| `T13` community listing | **13 471** | 367–371 |

What changed the number, precisely:

1. **The cheapest win from the first run was taken.** `node_id` is gone from
   the `query` mode's node records, and the answer says so —
   `answer_budget: {"omitted_fields": ["node_id"]}`. That is most of the
   3 335 → 2 811 drop on identical questions: a 16 % reduction with the
   omission declared rather than silent.
2. **Refusals became answers, and answers cost tokens.** `NEW-116` and the
   community work turned `T12` and `T13` from ceiling refusals into real
   answers. A refusal costs almost nothing and helps nobody; that trade is
   the right one. But a 20 026-token architecture summary and a
   13 471-token community listing are 25× and 36× what cbm spends on the
   same question, and `CLAUDE.md` §1 requires this product to spend tokens
   frugally in operation.
3. **`find_dead_code` now has a default budget (`0ca2fc8`) and it binds** —
   24 945 body tokens against a 25 000 ceiling, with `rows_omitted: 354`
   declared. That is why the `llm_wiki` column's `T06`/`T07` cost 24 982
   tokens each: to answer *is this one function dead*, the as-configured
   surface returns the whole listing. The `llm_wiki_best` column answers the
   same two questions for 194 and 191 tokens. The budget is honest; the
   question-to-tool fit is not.
4. `T13` has a 343-token answer available and the stand does not ask for it.
   `mode=community symbol=fuse_rrf` (`589a957`) returns the five-member
   community containing `scripts.retrieval.fuse_rrf` and
   `scripts.retrieval._fusion_weights` in 1 372 characters — exactly the
   `T13` gold. The stand's `T13` still calls `mode=community` with no anchor
   and gets the 13 471-token listing. That is a task-list gap, not a product
   gap, and I did not edit the task list to close it.

## Operator attention

| side | calls for 13 tasks | non-answering calls | fallbacks forced |
|---|---|---|---|
| `llm_wiki` | 14 | 1 | 1 |
| `llm_wiki_best` | 16 | 0 | 0 |
| `cbm` | 14 | 0 | 0 |

Down from 11 non-answers to 1 on the as-configured column — the single
largest improvement in this re-run. The one remaining is `T04`, and it is a
defect worth reporting on its own.

## Two defects, reported not patched

`scripts/` belongs to other agents in this session; I touched nothing there.
Both of these are one root cause with two faces.

**`find_dependencies` is handed a symbol name where a node id is expected.**
`_architecture_dependencies` (`scripts/mcp_server.py:1535`) and
`_architecture_symbol_dependencies` (`:1570`) forward `request["symbol"]` straight
into `find_dependencies(node_id=...)`, which validates it with
`_node_id()` against `_NODE_ID = [A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,511}`
(`scripts/evidence_graph.py:58`).

* **The loud face.** Any name beginning with `_` fails that first character
  class, so `mode=symbol` raises `node_id must use the closed delimiter-safe
  identifier syntax` for every private symbol. Measured: `mode=symbol
  symbol=fuse_rrf` answers correctly; `symbol=_page_diverse` errors. In a
  codebase where rule 5 mandates extracting private helpers, that is most of
  the symbols. This is `T04`, and it is the one non-answering call left.
* **The silent face.** A name that happens to pass the regex — including any
  path, such as `scripts/page_status.py` — matches no node and returns `[]`
  with no error. Combined with the empty `dependency` table, `mode=dependencies`
  cannot answer at all, and never says so.

The fix is not mine to write, but the shape is the one this vault already
chose twice: a zero must say which zero it is. An unknown symbol, an unknown
node id, and a symbol with genuinely no dependencies are three different
answers.

## Limits — what this run does not establish

* **Same 13 tasks, same repository, same machine, one day.** A single task
  moves any rate by 7.7 points. The `llm_wiki_best` 11 against cbm's 10–11 is
  a tie at this size.
* **Grading is mechanical string matching**, not comprehension, and the
  safety grades above are mine by hand, not the harness's. The harness's own
  `wrong_but_confident` counts any answered-but-wrong task and does not
  distinguish flagged from silent.
* **`tokens` is `len(text)//4`**, not a tokenizer.
* **Latency is load-dominated** and no in-session number for cbm's daemon was
  isolated.
* **The 81.4 % dead-code figure is a reference count, not an execution
  proof.** A value load proves that something names the function — which is
  exactly what `zero_confirmed_incoming_calls` denies — but it does not prove
  the registry entry is ever dispatched. Six were traced by hand to a real
  dispatch, and two (`_architecture_dependencies`, `_architecture_symbol`)
  were executed by this run.
* **`T12`/`T13` have soft gold**, and `T12` is the one cbm task that flips.
  The 10–11 spread rests entirely on it.
* **The code under test did not move**: `sha256` of
  `scripts/mcp_server.py`, `scripts/code_graph.py` and
  `scripts/graph_query.py` were identical before the first run and after the
  last (`a57a4589…`, `dc4f9cc7…`, `3c95d0b0…`). Repository `HEAD` did move,
  `5c1730d` → `fb45473`, because another agent committed during the session;
  none of that commit touched the three files. A concurrent agent also left
  `scripts/evidence_graph_builder.py` modified in the working tree near the
  end of the session; that file builds generations and cannot change an
  already-activated one, and every run above read the same activated
  generation.

## Does the register's conclusion still hold?

The register says, and asks to be repeated verbatim: *the evidence bar for
`CODE-07` is met, but it is too early to remove codebase-memory-mcp.*

**Both halves still hold, and the second one now holds for different
reasons.**

The evidence bar is met, more firmly than on 2026-08-28: five paired runs
against eight cbm runs, all five contract dimensions measured, spread
reported rather than a mean, and a demonstrated determinism property on our
side that cbm does not have.

Removing cbm is still premature, and the grounds have shifted:

* The 2026-08-28 grounds were *five documented modes refuse outright*. Those
  are largely gone — 0/13 became 8/13, and non-answering calls went 11 → 1.
* The grounds today are **correctness of what we do answer**.
  `mode=dependencies` has never worked on any published generation;
  `mode=symbol` fails for every private name; `find_dead_code` mislabels
  81 % of its defensible class. cbm answers `T08`/`T09` correctly and its
  `trace_path` answers who-calls in 44 tokens where we spend 346.
* And on cost, the direction is mixed rather than good: better on identical
  questions (3.4× → 2.75×), much worse overall (12.4×), because the newly
  unblocked answers are 20 000 and 13 000 tokens.

The one dimension where llm-wiki is now clearly ahead is stability: five runs
byte-identical against four-of-eight on cbm's summary, because an activated
generation is immutable and cbm re-indexes underneath the caller. For a
benchmark, and for an operator comparing two answers an hour apart, that is
worth something real. It is not enough to retire the other tool.

## Sources

* Run artifacts: `benchmark/code-parity-rerun-2026-08-29-run{1,2,3}.json`
  (all three columns), `-cbm{4..8}.json`, `-llm{4,5}.json`.
* Stand: `benchmark/run_code_parity.py` (unmodified); task list and hand gold
  `benchmark/code-parity-v1.json` (unmodified).
* New in this pass: `benchmark/aggregate_code_parity.py`,
  `tests/test_code_parity_aggregate.py`.
* First pairing: `docs/research/2026-08-28-code-parity-first-pairing.md`.
* The withdrawal being settled: commit `385f276`, register entry `NEW-124`.
* The changes tested: `a1b3d80` (`NEW-124`/`NEW-125`), `ddeb77e` (`NEW-135`),
  `589a957` (community anchor), `52e5673` / `c3f86c5` (`CODE-09`),
  `0ca2fc8` (dead-code budget).
* Register: `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, `CODE-07`,
  `NEW-116`, `NEW-124`, `NEW-135`.
