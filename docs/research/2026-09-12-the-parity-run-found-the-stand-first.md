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
that claims to have read it. Five tasks graded on a line number as a *required
term* (`"2845"`, `"1221"`, `"2765"`, `"1137"`, `"3007"`), so every side would
have graded `wrong` on those tasks whatever it answered, and the totals would
have looked like a product failure. One task, T07, asked whether
`_search_backends` is dead code; the H1 deletion had removed that function from
the tree, so the question measured nothing.

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

Files: `benchmark/code-parity-v2.json`,
`tests/test_parity_gold_resolves.py`,
`docs/research/2026-09-12-the-parity-run-found-the-stand-first.md`.
