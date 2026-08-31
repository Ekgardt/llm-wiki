# What the caller asked

2026-08-29. Why three of this product's code answers cost 12.4x
codebase-memory-mcp's, what is actually inside them, and what changed.

Measured against `benchmark/code-parity-v1.json` on this repository
(`/home/user/llm-wiki`, 29 459 nodes / 156 526 edges in the active generation).
Token counts are `len(text) // 4`, the same approximation
`benchmark/run_code_parity.py` uses throughout — an approximation, not a
tokenizer, and used only to compare shapes against each other.

---

## 1. The question this note answers

The brief named three answers and asked, for each: *what did the caller ask?*

The short version is that all three were answering a question nobody asked, and
each did it in a different way:

| answer | asked for | delivered |
|---|---|---|
| `get_architecture mode=summary` | "main modules and entry points" | entry points, a 100-row hotspot ranking, **and a 300-member community dump** |
| `get_architecture mode=community` | "which community holds `fuse_rrf`" | the 30 largest communities in the repository, none of them `fuse_rrf`'s |
| `find_dead_code` | "is `_flush_started` dead?" | 532 of 846 repository-wide candidates, 307 named ones silently cut |

---

## 2. Measurement before shaping

### 2.1 `mode=summary` — 20 143 tokens

| field | tokens | share | rows |
|---|---:|---:|---:|
| `communities` | 13 313 | 66.3% | 30 groups / 300 members |
| `hotspots` | 4 032 | 20.1% | 100 |
| `entry_points` | 2 617 | 13.0% | 97 |
| everything else | 22 | 0.1% | — |

Two thirds of an architecture summary is a community member dump. It is
**verbatim what `mode=community` already returns** — the same 13 313 tokens —
so two of the thirteen parity tasks were paying for one payload.

Inside the remaining third, more repetition:

* `entry_points`: `kind` and `name` were the constant `"main"` on all 97 rows —
  776 tokens (29.7% of that field) spent saying one word twice, 97 times.
* Every `file` on all 497 rows carried the absolute prefix
  `/home/user/llm-wiki/`, ~2 485 tokens, while `directory` and `source_root`
  name that root at the top of the same answer.

### 2.2 `mode=community` — 13 454 tokens, and it could not answer

The task asks which community holds `fuse_rrf`. The call passed no anchor. This
is not a tuning problem — `scripts/code_graph.py::_communities_holding` already
records why, and the docstring predates this work:

> A whole-graph listing cannot answer "which community does X belong to" at this
> repository's scale: naming all 17 194 members costs 899 071 estimated tokens,
> 36x the client ceiling, and `fuse_rrf`'s community is **729th by size**, so no
> honest bound reaches it.

Measured confirmation: the 2026-08-29 run graded **all three sides `wrong`** on
this task. The anchored form, which already existed, answers the same question
in **343 tokens** and names `scripts.retrieval.fuse_rrf` with its file and line.

### 2.3 `find_dead_code` — 24 982 tokens, and it was ambiguous

| field | tokens | share | note |
|---|---:|---:|---|
| `candidates` | 24 779 | 99.2% | 532 rows delivered of 846 |
| counts / report | 203 | 0.8% | |

Inside the 532 rows, 36.5% carried no information at all:

| column | tokens | why |
|---|---:|---|
| `status` | 3 059 | the constant `"candidate"`, 532 times |
| `graph_complete` | 3 325 | the constant `false` — **already at the answer's top level** |
| absolute path prefix | ~2 660 | `directory` names the root in the same answer |

The safety half matters more than the token half. `answer_budget` reported
`rows_omitted: 314, truncated: true`. Re-running the analysis without the
ceiling and diffing gives the exact figure: **307 named candidates were cut**.
So for those names the answer is *silence*, and silence in a listing reads as
"not a candidate", i.e. alive. The tool could not distinguish "X has living
callers" from "X was trimmed" — and `truncated: true` says rows were lost, not
which. That is a `wrong_but_confident` answer waiting to happen, in the one tool
whose whole job is a conservative claim.

---

## 3. What codebase-memory-mcp returns, and whether it answers less

It answers **less on two dimensions, more on the one the question named, and it
is honest about the difference.** All three are findings.

Its 817-token summary carries: node counts by label (14 kinds), edge counts by
type (22 kinds), languages by file count, **`packages` ranked by node count with
fan-in/fan-out**, and the top 20 entry points. Then this line:

> `aspects_hint: "Summary view (default). More on request via aspects:[...] —
> structure, dependencies, routes, hotspots, boundaries, layers, clusters,
> file_tree — or ["all"] for everything."`

* **Less**: 20 entry points against our 97; no hotspot table by default (it is
  an opt-in aspect); clusters likewise opt-in.
* **Better**: the question asks for *main modules*. cbm has a module ranking.
  We have none — our communities are unnamed numeric clusters, and our hotspots
  rank functions. On the literal thing asked, cbm answers and we do not.
* **The structural difference**: cbm's default is counts plus a bounded top-N,
  with a named menu of what else can be requested. Ours was counts plus three
  full listings, one of which was another mode's entire answer.

Its clusters aspect (361 tokens, 12 clusters) is one **row per cluster** — id,
label, member count, cohesion, five representative node names, packages, edge
types — not a member dump. Ours was 30 groups x 10 members with file and line
each.

This matches the dominant 2026 guidance for MCP responses: return only the
fields the agent needs and use progressive disclosure, with reported reductions
of 80–90% on over-broad payloads.

---

## 4. What changed

### 4.1 A constant column is stated once (`scripts/answer_budget.py`)

A value identical on every row of a table is a fact about the table, not about a
row. It now moves to a sibling `<key>_row_constants` exactly once. This is
lossless — the value is still in the answer — and it is the columnar move TOON
and every CSV-shaped encoding make.

Applied **only where it pays**, decided by measuring both shapes rather than by
a threshold on row count: a short table whose constants block would cost more
than it saves is left byte-identical. Comparison is type-strict, so a column
mixing `False` and `0` is not called constant.

### 4.2 The summary stops carrying another mode's answer (`scripts/mcp_server.py`)

`mode=summary` keeps every community *count* and replaces the member listing
with the string naming the mode that serves it, including the anchored form.
cbm's `aspects_hint` pattern, and for the stronger of two reasons: the listing
it dropped could not answer the question callers ask of it anyway.

### 4.3 `find_dead_code` takes a `symbol` (`scripts/mcp_server.py`)

A verdict about one name — `candidate` / `not_a_candidate` — with only that
name's rows, plus the repository-wide counts for context. The whole-repository
listing is unchanged for the caller who wants the audit.

`not_a_candidate` is deliberately not "alive": it says this analysis did not
nominate the name, and `graph_complete` in the same answer says how far that
carries. No new claim is made.

### 4.4 The second mint of opaque identifier (`scripts/answer_budget.py`)

`answer_budget`'s first doctrine is "opacity is decided by the value, not the
key", but its pattern knew only `code:<kind>:<32 hex>`. The stored generation
mints a second form for modules —
`repository:<64 hex>\x1f<language>\x1f<name>\x1f<path>` — which slipped through
because it *ends in readable text*.

This surfaced mid-task. `mode=dependencies` (T08/T09) had been failing at 107
tokens in the baseline run and started answering after another agent's
generation-invalidation fix, at 23 849 tokens. Measured composition of that
answer, 326 rows:

| field | tokens | share |
|---|---:|---:|
| `identity_key` | 14 564 | 61.1% |
| — of which the constant `repository:<64 hex>` prefix | 6 112 | 25.6% |
| `metadata` (`name`, `path`) | 7 962 | 33.4% |
| `depth` | 978 | 4.1% |

The key restates in an internal encoding what the same row already says in the
open. It is now dropped **only when that is demonstrably true of the row in
hand**: every readable segment of the key must already appear as a value in the
same row's `metadata`. A key holding anything the row does not otherwise say is
left alone, so the rule can never be the reason a fact left the answer.

---

## 5. Task-list changes, and why they are fixes to the question

Three calls changed in `benchmark/code-parity-v1.json`. **No question and no
gold moved.**

* **T13.** The question names `fuse_rrf`; the call did not pass it. All three
  sides graded `wrong`, and `_communities_holding` records why no unanchored
  listing can answer it. Each side is now anchored through its own best surface:
  `mode=community symbol=fuse_rrf` for llm-wiki, and `search_graph` for cbm —
  whose `get_architecture` takes no symbol or focus argument, verified against
  `cli get_architecture --help`.
* **T06 / T07.** The question asks whether one function is dead; the call
  requested every candidate in the repository, 307 of which were cut. llm-wiki
  now passes `symbol`. **cbm is untouched** — its `trace_path` was already
  anchored on the symbol, so this removes an asymmetry rather than creating one.

cbm's numbers on the changed tasks are reported in full in section 6.

---

## 6. Results

Paired rerun, `benchmark/code-parity-2026-08-29-anchored-run2.json`, against the
2026-08-29 baseline `benchmark/code-parity-rerun-2026-08-29-run3.json`.

### The three named answers

| call | before | after | cbm |
|---|---:|---:|---:|
| `mode=summary` (T12) | 20 026 | **6 107** (−69.5%) | 826 |
| `mode=community` (T13) | 13 471 | **343** (−97.5%) | 45 |
| `find_dead_code` (T06) | 24 982 | **250** (−99.0%) | 30 |
| `find_dead_code` (T07) | 24 982 | **248** (−99.0%) | 31 |
| **total** | **83 461** | **6 948** (−91.7%) | 932 |

### Whole stand

| side | grades before | grades after | tokens before | tokens after |
|---|---|---|---:|---:|
| llm_wiki | 8 correct / 0 partial / 5 wrong, 1 unanswered | **11 / 1 / 1, 0 unanswered** | 87 527 | **40 801** |
| llm_wiki_best | 11 / 0 / 2 | 11 / 1 / 1 | 36 382 | 9 130 |
| cbm | 11 / 0 / 2 | 11 / 1 / 1 | 2 427 | 2 114 |

`wrong_but_confident` on the llm_wiki side: **4 → 1**. Overall ratio against
cbm: **36.1x → 19.3x**.

---

## 7. Honest remainder

* **T04 lost a grade on all three sides, and it is not this work.** Its gold
  requires both `retrieval.py` and the literal `2765`. `_page_diverse` now sits
  at line 2897 and `fuse_rrf` at 1474, moved by commit `ae0fab5` while this task
  was running. The stand is measuring a repository other agents are editing.
  The gold was deliberately not corrected here: that is a separate matter, it
  touches several tasks, and moving gold mid-measurement is exactly what should
  not be done casually.
* **T09 is still 20 735 tokens, and the reason is not redundancy.** The question
  asks which *modules* `scripts/retrieval.py` depends on; the mode answers at
  symbol granularity. Of its 346 rows, 59 are modules, 272 functions and 15
  classes. `kind` genuinely varies, so the constant-column rule correctly leaves
  it alone. The fix is a `kind` filter on the mode's contract, which belongs to
  whoever owns that contract.
* **Relative paths were measured and deliberately not taken.** Absolute prefixes
  cost ~985 tokens of the summary and ~2 660 of the dead-code listing. `file` is
  in `PROTECTED_FIELDS` precisely because the `file:line` citation is what the
  answer exists to deliver, and rewriting citation values has a blast radius
  across snippet, provenance and LSP paths. 16% of one answer is not worth that
  in this change.
* **The summary is still 7.4x cbm's**, and 66% of what remains is the 100-row
  hotspot ranking. cbm makes it an opt-in aspect. Whether to bound it is a
  contract decision about what a summary promises, not a cleanup.
* **We have no module ranking.** cbm's `packages` block answers "main modules"
  literally and we do not. Named, not built.
* **`find_dead_code` unanchored barely moved** (24 982 → 24 966) — correctly.
  It is budget-bound, so compaction bought rows rather than tokens: **532 → 729
  rows delivered, `rows_omitted` 314 → 117**. Same price, 37% more of the answer.

## 8. Verification

* Managed gate `/etc/claude-code/enforcement/gate_complexity.py`: exit 0 on
  every edited file (`scripts/answer_budget.py`, `scripts/mcp_server.py`,
  `tests/test_answer_budget.py`, `tests/test_mcp_server.py`,
  `tests/test_dead_code_answer_default_budget.py`).
* `uv run ruff check scripts/ tests/ benchmark/` — all checks passed.
* `tests/test_mcp_server.py`: **359 passed, 1 skipped** (358 before, plus one new
  test for the symbol-anchored verdict).
* All 29 suites importing the changed modules: **1300 passed, 31 skipped,
  1 xfailed, 0 failed**.

### Two fixtures were made realistic, not weakened

Five tests failed after the constant-column rule, all for one reason: their
fixtures gave every row the *same* `file`/`owner`/`path`, which no real answer
does — the live dead-code answer has 93 distinct files across 532 rows. The fake
constancy compacted those fixtures under their budgets, so the trim and the
derivable-field drop stopped happening and the tests passed by never reaching
the ladder they exist to prove. The fixtures now vary those columns at roughly
the live ratio, and the budget in
`test_the_budget_drops_the_derivable_field_before_a_row` is now derived rather
than chosen: the fixture costs 249 tokens compacted and 191 once `owner` goes,
so any budget in [239, 297) is exactly the window where dropping the derivable
field is both necessary and sufficient. The old 300 sat outside it.

No assertion was relaxed.

## 9. One defect found in this work, recorded because it will recur

The first implementation named its predicate `_is_row_list`. That name was
already taken further down `answer_budget.py` by the trimming path's own
predicate, so the later definition won silently and **every code answer began
returning `operation_failed`** — caught only because the measurement was rerun,
not because a test named it. This is the same defect the vault log records for
`doctor.py::_require_real_directory` on 2026-08-22. When adding a helper to a
long module, grep the name first.

## Sources

- [How to Reduce Token Usage in AI Agents: 10 MCP Optimization Techniques — MindStudio](https://www.mindstudio.ai/blog/reduce-token-usage-ai-agents-mcp-optimization)
- [MCP Token Optimization: 4 Approaches Compared — StackOne](https://www.stackone.com/blog/mcp-token-optimization/)
- [How to Optimize MCP Server Token Usage: Code Execution, Tool Search, and TOON — MindStudio](https://www.mindstudio.ai/blog/optimize-mcp-server-token-usage)
- [10 strategies to reduce MCP token bloat — The New Stack](https://thenewstack.io/how-to-reduce-mcp-token-bloat/)
- [Engineering MCP tools for token efficiency — Pydantic](https://pydantic.dev/articles/engineering-mcp-tools-for-token-efficiency)
- [Progressive Tool Discovery for Token Efficiency — modelcontextprotocol discussion #1923](https://github.com/modelcontextprotocol/modelcontextprotocol/discussions/1923)
- `codebase-memory-mcp cli get_architecture --help` and its `aspects_hint`, read on this machine 2026-08-29.
