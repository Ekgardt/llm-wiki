# The first honest parity numbers

Dated 2026-09-12. Three runs of `benchmark/code-parity-v2.json` (16 tasks) and
three of `benchmark/code-parity-cross-service-v1.json` (2 tasks), llm-wiki
against codebase-memory-mcp, graded by the rule written before the numbers were
read (`docs/research/2026-09-12-when-we-would-drop-the-other-tool.md`).

## How it was run, including what is not ideal about it

Both sides answered about the **same checkout**, `/home/user/llm-wiki-tasks` at
`42f4f03`: llm-wiki through a repository-scoped generation built by
`scripts/repository_index.py index … --roots scripts tests benchmark
integrations` (551 Python sources, 29 570 nodes), codebase-memory-mcp through
its own index of the same directory (38 791 nodes, 189 725 edges). The stand ran
from that worktree with `LLM_WIKI_STATE_ROOT=/home/user/llm-wiki`.

Not ideal, and named rather than hidden:

- The measurement is **not the installed vault**. `/home/user/llm-wiki` cannot
  answer about its own code at all — see
  `docs/research/2026-09-12-the-parity-run-found-the-stand-first.md`. The
  worktree stands in for it.
- Per-call cost includes each side's real process start, as the stand's
  docstring says, and `tokens` is `len(answer)//4`, not a tokenizer.
- The cross-service fixture was indexed for both sides while nothing else ran.
  One indexing of the two fixture repositories overlapped the start of v2 run 3;
  its per-task seconds match runs 1 and 2 within 0.5 s, so nothing was redone.

## The 16-task set, three runs

| side | correct (of 16) | tokens | total seconds | p95 per task | confident-wrong | non-answers |
|---|---|---|---|---|---|---|
| `llm_wiki` | 13, 13, 13 | 9 869 | 96.6 / 101.0 / 95.9 | 12.70 s | 1 | 0 |
| `llm_wiki_best` | 14, 14, 14 | 7 685 | 92.2 / 90.7 / 92.3 | 10.15 s | 1 | 0 |
| `cbm` | 15, 15, 15 | 5 565 | 37.2 / 40.0 / 37.4 | 4.18 s | 1 | 0 |

Every grade was identical in all three runs; token counts were identical to the
byte. The variation is entirely in wall time.

Where the sides differ, task by task:

- **T05** (where is the constant `EDITORIAL_NAMES` defined) — cbm correct, both
  our columns **wrong** in every run. The generation indexes no module-level
  constant node, so the question has no surface to land on. This is the one task
  the other tool always answers and we never do.
- **T14** (argument binding into `_graph_seeds`) — cbm correct, ours **partial**
  in every run: we name the parameters but not in the `caller->callee` shape the
  gold asks for.
- **T04** (where is `_page_diverse` defined) — `llm_wiki` partial, `best`
  correct: the `callers` surface answers without a line number, the `query`
  surface with one.
- **T16** (which tests exercise `fuse_rrf`) — **cbm wrong**, both our columns
  correct, in every run. This is the only task we win.

## The cross-service set, three runs

| task | `llm_wiki` | `llm_wiki_best` | `cbm` |
|---|---|---|---|
| X01 — which service function handles the request | correct, 0.58–0.68 s | correct, 0.57–0.81 s | **partial**, 1.92–2.06 s |
| X02 — what the handler stores | wrong, ~0.6 s | wrong, ~0.6 s | wrong, ~2.0 s |

X01 is ours: the answer names the route `POST /orders`, the handler
`orders.api.create_order` and the repository it lives in, in under 0.7 s. X02 is
the boundary the 2026-09-11 decision drew on purpose — a cross-repository hop
names the handler and stops; it does not open the other generation. Both sides
fail it, so it measures a gap rather than a defect, exactly as the task file
says it should.

## The verdict against the rule

The rule had four conditions. Taking `llm_wiki` as our score, because that is
the surface an agent reaches:

1. **Correctness — fails.** 13 against 15, and T05 is a task the other tool
   answers in every run while we answer it in none.
2. **Safety — passes.** One confident-wrong answer each.
3. **Attention — passes.** Neither side ever failed to answer. Both are far from
   the 2026-08-28 pairing, where our side scored 0 of 13.
4. **Cost — fails.** Tokens 1.77× (`best`: 1.38×) against a 1.5× ceiling, and
   p95 per task 3.0× (`best`: 2.4×) against a 2× ceiling.

**So codebase-memory-mcp stays installed.** Two of four conditions fail, and one
of the failures is a whole question shape we cannot answer. The gap is not
large — one task of correctness, and a 2.4× to 3× latency that is mostly the
per-call process start the stand deliberately does not amortise — but the rule
was written to be applied, not admired.

What would close it, in the order the numbers point:

1. Index module-level constants in the generation (T05, the only task we always
   lose).
2. Return the binding in `caller->callee` form for argument-binding answers
   (T14).
3. Carry the line number on the `callers` surface, not only on `query` (T04).
4. Cut the per-call cost: 12.7 s p95 against 4.2 s is dominated by importing
   `mcp_server` per call in the stand's child process, which is the true cost for
   a caller that starts fresh and not the cost inside a warm MCP session. A
   measurement of the warm path would be a different, also honest, number — and
   it is not this one.

## Sources

`benchmark/code-parity-v2-2026-09-12-run{1,2,3}.json`,
`benchmark/code-parity-cross-service-2026-09-12-run{1,2,3}.json`,
`docs/research/2026-09-12-when-we-would-drop-the-other-tool.md`,
`docs/research/2026-09-12-the-parity-run-found-the-stand-first.md`.

Files: `benchmark/run_code_parity.py`, `benchmark/code-parity-v2.json`.
