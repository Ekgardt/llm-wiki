# The parity run found the stand first

Dated 2026-09-12. The owner's word was «Да, но после зелёного CI», and CI went
green on 53 of 53 checks, so the run started. It produced no comparison. It
produced three defects, two of them in the product and one in the benchmark,
and every one of them would have been invisible in a published table of
numbers.

## Sources

1. The run itself: `benchmark/run_code_parity.py` against
   `benchmark/code-parity-v2.json`, first task only. `llm_wiki` timed out at
   70.13 s; `llm_wiki_best` returned `tool_error` in 0.57 s; `cbm` answered
   correctly in 1.98 s.
2. The generation catalog on the installed vault,
   `cache/evidence-graph/catalog.sqlite3`, and the three generations it still
   holds. Counted with SQL: the generation activated 2026-09-07 holds 1 113
   sources, 546 of them code, and 35 630 nodes; the ones activated 09-11 and
   09-12 hold 309 and 322 sources, **zero** code, and about 14 400 nodes.
3. `logs/maintenance/20260912T030233-repositories-1816302.out.log` — the
   nightly's repository step, verbatim: for `/home/user/llm-wiki`,
   `"status": "refused"`, `"reason": "repository_is_the_vault"`, message *"this
   is the vault itself; its generation is built and activated by the nightly
   pass"*.
4. `scripts/doctor.py::run_generation_maintenance` docstring and
   `scripts/corpus_snapshot.py:51` — `VAULT_CODE_ROOTS: tuple[str, ...] = ()`.
   Commit `780f91b` (2026-09-10, «the memory index holds memory only», #29.2)
   is what emptied it.
5. `git show 28c46b6:scripts/retrieval.py` — the tree at the commit whose
   message says the v2 gold was *"read by hand from the working tree on
   2026-09-11"*.

## Facts

**One. Nothing indexes this repository's code any more.** After #29.2 the
vault's own generation collects `knowledge/` alone — that was the decision, and
it is a good one. Code was to live in repository-scoped generations. But the
repository step refuses this checkout with `repository_is_the_vault`, and its
stated reason is that *the nightly pass builds it* — which stopped being true in
the same commit that emptied `VAULT_CODE_ROOTS`. So since 2026-09-11 no active
generation on this machine holds a single function of this repository. The
measured consequence: `get_architecture mode=query` for `fuse_rrf` with zero
hops returns `"nodes": []` — the start node itself is not there — and the
structural modes fall back to live extraction, which is the 70-second timeout
the run recorded. Two halves of one contract, each correct alone, and a hole
between them.

**Two. The v2 gold described a tree from 2026-08-28.** Eight of sixteen tasks
cited line numbers that resolve to nothing: `scripts/retrieval.py:1378 (def
fuse_rrf)` when `fuse_rrf` is at 1568 — and it was at 1568 in the very commit
that claims to have read it.

Correction to my own first reading of this, made before I read the grader:
`_found` treats a nested list as **alternatives**, not as a conjunction, so
`["_fused_candidates", "2845"]` is satisfied by naming the function alone and a
stale line number inside a pair costs nothing. Five tasks were *not* made
ungradeable, as I first wrote. Two were:

- **T04** grades on `["retrieval.py", "2765"]` — two separate entries, both
  required — and `_page_diverse` is at 3030, so no correct answer could earn
  the second.
- **T10** requires `["retrieve", "3007"]` as one of two entries, and `retrieve`
  is not a caller of `_fused_candidates` in this tree at all; the callers are
  `_partial_candidates` and `_executed_plan`. An honest answer could not match
  it.

One more task, T07, asked whether `_search_backends` is dead code; the H1
deletion had removed that function from the tree, so the question measured
nothing. The rest is an evidence defect rather than a grading one: every
citation is the gold's proof, and a proof that points at the wrong line is not
a proof.

**Three. The collector refuses this repository by its own bound.** Indexing
`/home/user/llm-wiki-tasks` with default roots is refused with *duplicate corpus
source path: `knowledge/notes/2026-04-13 Three Conventions One Root.md`*, and
there is exactly one file of that name on disk. A repository that is also a
vault reaches `knowledge/` twice and the collector counts the collision as the
repository's fault.

## What was changed here, and what was not

Changed, because it is fact-keeping and not design:

- Every line number in `benchmark/code-parity-v2.json` re-read from the tree by
  `grep`/AST on 2026-09-12, including the two-hop task, whose gold claimed
  `retrieve` as the only second-hop caller when the callers are
  `_partial_candidates` (:3235) and `_executed_plan` (:3344).
- T07 retired with its reason recorded in the file, and replaced by the same
  question about `_legacy_vector_source_membership`
  (`scripts/search_memory.py:5020`), whose name occurs exactly once in the whole
  repository — its own `def`.
- `tests/test_parity_gold_resolves.py`: every gold citation must resolve in the
  tree, every graded line number must be one the citations name, and a retired
  task must state why it left. This is the loud failure the stand lacked; a
  stale benchmark does not crash, it publishes.

Not changed, because it is the owner's decision: **which generation holds this
repository's code.** Two ways out, and they are not equivalent. Either the
repository step stops refusing the vault's own checkout and builds it a
code generation beside the memory one — one directory, two generations, which
matches the "one directory, two audiences" contract already in `CLAUDE.md` — or
the vault generation takes code roots again and #29.2 is narrowed to mean the
*search* corpus rather than the graph. The first keeps memory and code
separable, costs one more generation on disk, and leaves #29.2 intact; the
second is smaller but re-mixes what was just deliberately separated. I did not
choose.

## Open

The comparison numbers do not exist yet. To produce them tonight without
touching the contract, the run is pointed at the worktree checkout
`/home/user/llm-wiki-tasks`, which the repository step does index (it is not the
vault), with explicit code roots to step around defect three. That measures the
same code through the same product path. It is a workaround and it is named as
one: the vault itself still answers nothing about its own code.

## A fourth defect, mine, found by CI

`timing::macos_full::py3.10-s2` failed on `7a457f2`. The shard holds
`tests/test_code_parity_stand.py`, where the test I added asserted
`default_cbm_project("/home/user/llm-wiki") == "home-user-llm-wiki"` — a literal
that holds only on this Linux box. `/home` is an autofs mount on macOS and a
drive-anchored path on Windows, so `Path(...).resolve()` returns something else
there and the assertion is false; the Windows shard would have failed the same
way once it ran. The job log was not readable while the run was still in
progress, so the cause is identified by inspection of that shard's contents, not
from the traceback — CI's rerun is the confirmation.

Both halves are fixed rather than the one that failed: the derivation now joins
the path's parts after its anchor, so no separator and no drive letter can
survive on any platform, and the test asserts the dashing on a nested `tmp_path`
plus the absence of separators, with no machine path in the expectation.

Files: `benchmark/code-parity-v2.json`,
`tests/test_parity_gold_resolves.py`,
`tests/test_code_parity_stand.py`,
`benchmark/run_code_parity.py`,
`docs/research/2026-09-12-the-parity-run-found-the-stand-first.md`.
