# NEW-111 was fixed sixty-seven minutes after it was recorded

**Filed:** 2026-08-29. **Measurements taken:** 2026-08-28 22:40 – 2026-08-29
00:20 UTC on `/home/user/llm-wiki`, HEAD `25e2e73`.
**Kind:** verification of an open audit item. **No product code was changed.**

One-sentence summary: `EvidenceGraph.open_active_for_repository` no longer fails
on this vault — the defect the audit registry still calls undiagnosed was
diagnosed and fixed by commit `60f7d6c`, which landed 67 minutes *after* the
commit that filed it — and the same trap is still live at two other sites, one
of them the gate behind the 643 s → 3.9 s rebuild win.

---

## What the registry says

`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, in the block headed
«Закрытия аудита 2026-08-27 (`3c12858`)»:

> `NEW-111`: `EvidenceGraph.open_active_for_repository` на этом хранилище
> падает через ~4 с с «active Evidence Graph changed while opening» — именно
> это загоняет код-инструменты в живой путь. Причина не разобрана.

## Why it is stale, measured

| commit | time (UTC) | subject |
|---|---|---|
| `3c12858` | 2026-08-27 01:48:58 | filed NEW-111 as undiagnosed |
| `60f7d6c` | 2026-08-27 02:55:41 | *fix: open the immutable generation despite a moved HEAD* |

`git merge-base --is-ancestor 60f7d6c 3c12858` → **NO**. The fix did not exist
when the registry line was written; nobody went back to strike the line. It was
true when written and has been false since 02:55 on 2026-08-27.

## The cause, and it is the third instance of a known trap

`60f7d6c` names it exactly: the open loop re-compared **whole** repository
scopes, and a scope carries `git_commit`. On a vault that commits its own
runtime the generation's build-time commit almost never equals the checkout's
current commit, so `_opened_repository_once` returned `_RETRY` on every attempt
and the three-attempt budget ended in
`PermissionError: active Evidence Graph changed while opening`. Meanwhile the
catalog admitted the same generation by identity (`same_repository`).

This is the same trap as `NEW-65` (generation eligibility) and `NEW-90`
(publication-root check). Identity is derived from paths; the commit is
provenance. The open site now asks `same_repository()`; publication still binds
the exact commit (`generation_catalog._require_publication_scope`, whose
docstring says so).

## The paired measurement, on this live vault

The trigger condition is still present, so the fix is doing real work rather
than being masked by a coincidence:

```
active generation : generation-18d015d5499fc78e-7d4cc1d8
generation commit : 3ad8f78ed274d2243031a7e82e2e0d1182d71042
live commit       : 25e2e73c28729c3854741d0df839a220710e27f3
whole scope equal : False        <- what the pre-fix code compared
same_repository   : True         <- what the code compares now
```

Arm B restores the pre-fix comparison by monkeypatch (whole-scope `!=` in
`_admitted_generation`, `_RETRY` on refusal); nothing else differs.

| arm | n | opened | failed | rate | min | max | mean |
|---|---|---|---|---|---|---|---|
| pre-fix comparison | 30 | 0 | 30 | **1.00** | 1.491 s | 4.458 s | 1.997 s |
| code as it stands | 30 | 30 | 0 | **0.00** | 1.194 s | 3.963 s | 1.487 s |

Every pre-fix failure carried the registry's exact message, and its 4.458 s
worst case matches the registry's «через ~4 с».

## The consequence the registry cared about

One real code question through `mcp_server._execute_tool_call`
(`get_architecture`, `mode=summary`, directory `/home/user/llm-wiki`):

```
arm=current  elapsed=5.31 s
  fallback          = False
  source_generation = 'generation-18d015d5499fc78e-7d4cc1d8'
  graph_complete    = False
  unresolved_count  = 79263
```

The same call with the pre-fix comparison restored never reaches an answer. It
falls into live extraction and raises `TimeoutError: MCP operation deadline
reached` — at the tool's own 120 s deadline on the first run, and again on a
second run given **1500 s**, which it also exhausted (25 min 01 s of wall
clock, no answer). So the registry's "this is what drives the code tools into
the live path" was correct, the live path on this vault does not finish inside
any budget a tool call has, and neither holds any longer:

| arm | deadline given | outcome |
|---|---|---|
| pre-fix comparison | 120 s | `TimeoutError` at the deadline |
| pre-fix comparison | 1500 s | `TimeoutError` at the deadline |
| code as it stands | 120 s | answered in **5.31 s**, `fallback: False` |

## The regression guard already exists

`tests/test_open_active_scope_identity.py`, five tests. With the pre-fix
comparison restored (pytest plugin, no source edited): **3 failed, 2 passed**,
each failure `PermissionError: active Evidence Graph changed while opening`.
On the code as it stands: **5 passed**. Two of the five pin the guarantees the
fix must not weaken — a torn artifact and a different checkout are still
refused.

## Found while checking for a fourth instance: the reuse gate has the same bug

`grep` for whole-scope comparisons turned up two more sites that compare a
recorded scope against a live-resolved one with `==`:

1. **`scripts/evidence_graph_builder.py:2076`**, `_reuse_config_matches` —
   decides whether the incremental build may reuse the parent generation's
   records. This is the gate behind `283eb3a` ("idle pass 643 s → 3.9 s"), and
   `git log -S` confirms that commit touched this function.
2. **`scripts/doctor.py:7005`**, `_parent_matches_identity` — decides whether
   the maintenance pass considers the active generation current.

Measured on this vault, calling the product's own `_reuse_config_matches` with
the real active manifest and changing only the scope argument:

```
parent config gate                                   : True
reuse with LIVE scope (what the build passes)        : False
reuse with the generation's OWN scope (control)      : True
```

The only difference between the two arguments is `git_commit`. The scope the
build passes is freshly resolved: `doctor.py:7132` calls
`resolve_repository_scope(root, ...)` and hands it to
`build_incremental_generation`, which converts it with `as_dict()` at
`evidence_graph_builder.py:1908`.

**Measured:** the gate flips on the commit alone.
**Inferred, not measured:** the first generation build after any commit
therefore reuses nothing and pays the full pass. I did not run a nightly build
to time it — that would publish a generation into the live vault.

`doctor.py` already knows the distinction one thousand lines earlier:
`_scope_state` (line 4041) carries a docstring saying that comparing the whole
scope "made every commit read as `mismatched`", and returns `superseded`
instead. Line 7005 was not brought along.

Both files belong to other agents right now, so this note reports them rather
than changing them.

## How an unfixable-here defect gets recorded: strict xfail, not prose

The choice for `tests/test_generation_reuse_scope_identity.py` was between a
paragraph in this note and an executable pin. Prose loses: the audit line for
NEW-111 is itself the proof — it stayed wrong for two days because nothing ran
it.

- **This vault's own precedent**, 2026-08-22 (`knowledge/log.md`, `NEW-56`):
  "Два теста написаны и помечены `xfail(strict=True)`" for exactly this shape —
  a defect understood and measured, whose fix was blocked from that agent.
- **pytest's documented semantics** for `strict=True`, checked against the
  current docs on 2026-08-29 rather than from memory —
  <https://docs.pytest.org/en/stable/how-to/skipping.html>: `strict=True`
  "will make `XPASS` ('unexpectedly passing') results from this test to fail
  the test suite", while an expected failure is reported `XFAIL` and does not
  fail the run. That is the property wanted here — the pin cannot rot quietly,
  and whoever fixes either site is told to remove it. The same page documents
  the `xfail_strict = true` ini option, which flips the project-wide default;
  not used here, because these two are the only strict xfails intended and
  changing a global default is not this task's decision to make.
- **Rejected: `skip`.** A skip proves nothing and never notices a fix.
  **Rejected: a plain failing test.** It would redden CI for every other agent
  over a defect none of them introduced.

Each pinned test ships with a control that passes today (identical scopes are
admitted), so an XPASS means the commit stopped mattering, not that the gate
stopped working.

## Source / evidence

- `scripts/evidence_graph.py::EvidenceGraph._admitted_generation` (the fix and
  its docstring), `::open_active_for_repository`
- `scripts/generation_catalog.py::_require_publication_scope` (publication
  still binds the commit — deliberate, unchanged)
- `scripts/repository_scope.py::RepositoryScope.identity` / `::same_repository`
- `scripts/evidence_graph_builder.py::_reuse_config_matches` (line 2076),
  `scripts/doctor.py::_parent_matches_identity` (line 7005) vs
  `scripts/doctor.py::_scope_state` (line 4041)
- `tests/test_open_active_scope_identity.py`,
  `tests/test_generation_reuse_scope_identity.py`
- commits `60f7d6c`, `3c12858`, `283eb3a`
- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` → `NEW-111`, `NEW-65`, `NEW-90`
- `knowledge/log.md`, entry 2026-08-22 (`NEW-56`, strict-xfail precedent)

## Related

- [[knowledge/notes/derived-evidence-generation-decision]] — generations are
  disposable and derived; identity, not provenance, decides membership.
