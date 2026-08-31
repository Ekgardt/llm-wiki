---
type: raw-source
status: active
confidence: high
source_authority: web
date: 2026-08-28
---

# Generations for other repositories

One-sentence summary: before letting the code index leave this checkout, this
note establishes what a repository *is* to a multi-repository indexer, how the
comparable tools hold many of them, and — measured on this machine — why the
identity this vault already derives is the better half of the answer and the
corpus collector's root allowlist is the part that still says "the vault".

Written for `CODE-03` (roadmap section 12), under rule 2, before the design
change. Everything dated 2026-08-28 was measured here today.

---

## 1. What the product already carries, and what silently assumes the vault

Traced through the graph (`home-user-llm-wiki`, index generation
2026-08-28T20:30:16Z, `check_index_coverage` reports no recorded gap for all
five files below) and confirmed by reading the source.

**Already repository-scoped — no change needed:**

| Place | What it does |
|---|---|
| `scripts/repository_scope.py` | `repository_id` from the canonical `git_common_dir`, `checkout_id` from the checkout root. Both SHA-256 over serialized absolute paths. |
| `RepositoryScope.identity()` | Deliberately excludes `git_commit` — `NEW-65`/`NEW-90`. The commit is provenance, not identity. That decision stands and this work does not touch it. |
| generation manifests | Every manifest carries `repository_scope`; `GenerationCatalog._registered_repository_scope` reads it back and `_scope_admits` compares by `same_repository`. |
| `cache/evidence-graph/generations/<id>/` | One immutable directory per generation. Nothing in the layout is vault-specific. |
| `code_graph._generation_catalog` | Opens the vault's one catalog and **explicitly discards its `directory` argument** (`del directory`), then `_active_graph_or_none` resolves the scope *of the requested directory* and asks the catalog for that scope. |
| `mcp_server._validated_code_directory` | Accepts any absolute, existing directory on the machine. No containment to the vault at all. |

That last pair is the important discovery: **the read path is already
multi-repository end to end.** Point `get_architecture` at another repository
today and it resolves that repository's scope and asks the one shared catalog
for a generation belonging to it. Nothing is hardcoded to this checkout.

**Silently assumes "the vault" — three places, in order of how much they hurt:**

1. **`GenerationCatalog._active_attempt`** — one singleton pointer.
   `catalog_state.active_generation_id` is a single row. `_scope_admits(active,
   scope)` returns False when the pointer names another repository's
   generation, and the caller then gets `return True, None` — settled, nothing.
   So *at most one repository at a time can have a resolvable generation*, and
   which one is decided by whoever activated last. This is the structural
   blocker for CODE-03 and it is inside a file this task may change.

2. **`corpus_snapshot.APPROVED_CODE_ROOTS`** — a frozen set of **this vault's
   own directory names**: `benchmark, docs, integrations, rules, scripts,
   skills, tests`. `_code_root()` refuses any code root outside it, so
   `collect_corpus` cannot be pointed at a repository whose code lives in
   `src/`, `lib/`, `pkg/`, `cmd/` or `app/`. Measured today against the real
   second repository on this machine: `ValueError: code root must be a
   normalized approved relative POSIX path` for `src`, `ops`, `seed`, `vepkit`,
   `config`, `fixtures`, `.github`. The dataclass `SnapshotPolicy` itself has
   no `__post_init__` and validates nothing — the allowlist is enforced only in
   `_policy`/`_code_root`, i.e. it is a *call-site policy wearing the costume
   of a format rule.* `scripts/corpus_snapshot.py` is out of this task's file
   boundary, so this is reported, not changed. See §6.

3. **`doctor._approved_code_roots(root, APPROVED_CODE_ROOTS)`** — the vault's
   own build passes the intersection of the allowlist with what exists. That
   one is correct *as a caller's policy*; it is only wrong that the callee
   refuses to be told anything else.

---

## 2. How the comparable tools hold many repositories

**Sourcegraph / Zoekt.** Zoekt splits each repository's index into one or more
files on disk called *shards*, memory-mapped at query time; Sourcegraph shards
repository indexes across replicas and rebalances when the replica count
changes. `Repository.ID` and `Repository.TenantID` are Sourcegraph-specific
extensions "used for filtering and isolation". Small shards are merged into
compound shards to cut per-shard overhead. The lesson is not the sharding —
this product has no replicas — it is that **the repository is the unit of the
artifact and the unit of isolation, and identity is an id carried inside the
artifact, not the directory the artifact happens to sit in.** That is exactly
what a generation manifest's `repository_scope` already is.

**SCIP / LSIF.** An index is produced per repository and uploaded per commit;
cross-repository navigation works by resolving a symbol id found in one
repository's index against the index of the repository that defines it. SCIP
supports incremental indexing so that after a push only changed files are
re-indexed. Two lessons: **(a)** the upload is addressed by *(repository,
commit)* — the commit says what this index was made from, it is not what makes
it that repository's index, which matches this vault's `NEW-65` finding
independently; **(b)** cross-repository resolution is a *symbol-id* join, not a
merged graph. This work does not attempt (b) — see §5.

**codebase-memory-mcp, on this machine, today.** This is the tool the product
contract says an operator must be able to drop, so its registry is the most
relevant evidence available — and it is a cautionary tale. `list_projects`
returns **23 projects, 1.2 GB in `~/.cache/codebase-memory-mcp`**, of which
**20 are the same repository**: `agenticos-ga11a`, `agenticos-ga19a`,
`agenticos-xc02`, `agenticos-de06`, … each one an agent worktree under
`.claude/worktrees/`, each carrying its own ~14,000-node, ~48-50 MB index of
substantially identical code. The registry keys on the path, so:

- one repository becomes twenty projects;
- when the derived name collides, the key degrades to the whole path
  (`home-user-agenticos-checkout-claude-main-.claude-worktrees-agent-a741bf753f9768152`);
- a *subdirectory* can be registered as a peer project — `llm-wiki-scripts`,
  root `/home/user/llm-wiki/scripts`, sits in the same list as `home-user-llm-wiki`
  and re-indexes 7,408 nodes of the 28,000 already in its parent.

Measured against the same three directories, `resolve_repository_scope` gets
this right without being asked:

```
/home/user/agenticos/checkout-claude/main                      repository:47d7e17166d3b6f48…
/home/user/agenticos/checkout-claude/main/.claude/worktrees/…  repository:47d7e17166d3b6f48…
/home/user/agenticos/checkout-claude/fix-pip                   repository:47d7e17166d3b6f48…
/home/user/llm-wiki                                            repository:d8c142988412f9d85…
```

Same repository id for all three agenticos checkouts (they share
`git_common_dir = /home/user/agenticos/checkout-claude/main/.git`), distinct
`checkout_id` for each. **The identity half of CODE-03 is already built and is
better than the tool it has to replace.** What is missing is only that the
catalog can hold a generation for one repository at a time.

---

## 3. What identifies a repository across moves and worktrees

Git's own layout answers this. Most files under `$GIT_DIR` are per-worktree;
files under the *common* directory are shared by every working tree, and a
linked worktree's `.git` is a file whose `commondir` points at the shared
directory. `git rev-parse --git-common-dir` therefore returns one value for the
main checkout and for every linked worktree of it. That is what
`derive_repository_id` hashes.

Three consequences, and they are the ones that matter for this feature:

- **A worktree is not a new repository.** It is a second checkout of one. This
  matches the decision recorded here on 2026-08-26 — an agent worktree is a
  temporary copy of a project, not a project — and it is the exact defect
  visible in cbm's registry above.
- **A move renames the identity.** `repository_id` is derived from a path, so
  moving a repository produces a new id and its old generations stop being
  eligible; they become garbage the existing cleanup collects. That is a
  deliberate trade already made by `repository-scope/v1` and this work does not
  reopen it. The alternative — keying on the remote URL — is worse for a
  local-first product: a repository need not have a remote, may have several,
  and two unrelated checkouts of the same remote would collide.
- **The commit is not part of it.** Settled by `NEW-65`/`NEW-90`.

---

## 4. The isolation boundary

**One catalog, many repositories, no shared rows.** A generation is already
sealed to its repository by the manifest; the catalog's `_scope_admits` already
refuses to hand a generation to a scope it does not belong to; `EvidenceGraph`
re-checks the scope after opening. Nothing merges across repositories: a query
about repository B can only ever read B's generation database.

The one thing that must change is *selection*, and it must change in a way that
cannot damage the vault. The rule adopted:

> The singleton active pointer belongs to the vault. A foreign repository is
> resolved by scanning the registered generations for its scope and taking the
> newest — a read that never moves the pointer, never writes activation
> history, and never repairs anything.

This is deliberately asymmetric, and the asymmetry is the safety property. If
foreign generations were activated instead, then indexing another repository
would move the pointer, and the vault's own `get_active_for_repository` would
immediately answer `None` for the vault — every knowledge query would fall back
to the legacy index. That is `NEW-65` re-created on purpose. So: foreign
generations are **registered, never activated**. `build_full_generation` and
`build_incremental_generation` already take `activate=False` and, on that path,
do not require a `publication_root`, so no new build mode is needed.

**A foreign repository is read-only to this product.** Nothing is written into
it — no config, no marker, no lock. The generation is written under this
vault's `cache/evidence-graph/`, which is disposable derived cache by the
standing contract, and deleting it costs a rebuild and nothing else. No new
runtime root, no second catalog, no second graph, no daemon.

---

## 5. What this will NOT support, and why

- **Cross-repository symbol resolution.** SCIP does it by joining symbol ids
  across per-repository indexes. Doing that here would mean a second, joined
  graph — which the contract forbids and which nothing has asked for. Each
  repository answers about itself; a call from A into B is unresolved in A's
  graph exactly as it is today.
- **Activating a foreign generation.** §4. There is one active pointer and it
  is the vault's.
- **Watching or auto-refreshing foreign repositories.** cbm auto-refreshes
  watched projects in the background; that needs a daemon this product does not
  have and does not want. Freshness here is a question the operator asks
  (`detect`), not a promise the product makes.
- **Indexing a subdirectory as a repository.** The `llm-wiki-scripts` mistake.
  The requested directory must be the checkout root, which the scope resolver
  can already prove.
- **Indexing a submodule as its own repository.** Its files belong to the
  superproject's tree; two generations claiming overlapping paths is a
  contradiction with no reader to resolve it. Refused by name, and the
  superproject is named in the refusal.
- **Indexing anything not owned by the caller,** including another user's
  home. There is no privilege boundary in a local-first single-operator
  product, so ownership is the only boundary available; POSIX `st_uid` is it,
  and on a platform where that is meaningless the receipt says the check was
  not performed rather than pretending it passed.
- **Repositories larger than the corpus bounds.** `collect_corpus` already
  fails closed on `max_files` / `max_total_bytes` / `max_entries`. The refusal
  is surfaced by name instead of as a traceback. Nothing is silently truncated:
  a half-indexed repository is worse than an unindexed one, because *absent*
  then reads as *does not exist* — which is precisely `NEW-67`.

---

## 6. The one honest hole, named in advance

Because `corpus_snapshot.APPROVED_CODE_ROOTS` is out of this task's file
boundary, `index` can only collect code roots whose **names** are in the
vault's own allowlist. Against the real second repository on this machine that
means `docs`, `scripts`, `skills`, `tests` are indexable (436 sources, 293
Python files, collected in 1.05 s) and `src` (374 files — the actual product
code), `ops`, `seed`, `vepkit`, `config`, `fixtures`, `.github` are not.

Two design consequences follow, and both are chosen rather than tolerated:

1. **Default is fail-closed.** With no explicit roots, `index` discovers the
   git-tracked top-level directories and refuses the whole index if any of them
   is outside what the collector will accept, naming them. A partial index that
   nobody was told about would make `find_callers` answer "none" for a symbol
   with fifty callers.
2. **The operator can narrow deliberately.** Passing `roots` explicitly indexes
   exactly those, and the covered roots are recorded in the generation's own
   policy, so every later answer can say what was in scope.

The precise change that would close the hole, for whoever owns
`scripts/corpus_snapshot.py`:

> `_code_root()` (line ~769) should take the admissible prefixes as an argument
> instead of closing over the module constant, exactly as `_relative_posix`
> already takes `prefixes`. `collect_corpus`/`_policy` would grow an
> `approved_code_roots: frozenset[str] = APPROVED_CODE_ROOTS` parameter. The
> path-shape validation — normalized, relative, POSIX, no traversal, NFC — is
> the real invariant and stays untouched; only the *name* allowlist becomes the
> caller's. `SnapshotPolicy` needs no change: it already validates nothing, and
> the manifest already records `code_roots` verbatim, so a generation would
> keep saying exactly which roots it covered.

Why not work around it from outside: constructing a `SnapshotPolicy` directly
and calling `corpus_snapshot._capture` would bypass the *only* place the
path-shape invariants are checked, to get around a name list. Copying files
into an approved-looking tree would break `checkout_root` identity and every
path in every answer. Both are worse than a named refusal.

---

## Sources

- [Sourcegraph architecture](https://sourcegraph.com/docs/admin/architecture) — repository index sharding across replicas, rebalancing.
- [zoekt package](https://pkg.go.dev/github.com/sourcegraph/zoekt) — shards, memory-mapping, `Repository.ID`/`TenantID` for filtering and isolation.
- [sourcegraph/zoekt (DeepWiki)](https://deepwiki.com/sourcegraph/zoekt) — `zoekt-git-index` producing one or more shards per repository; compound-shard merging.
- [Why code search at scale is essential when you grow beyond one repository](https://sourcegraph.com/blog/why-code-search-at-scale-is-essential-when-you-grow-beyond-one-repository)
- [Cross-repository code navigation](https://sourcegraph.com/blog/cross-repository-code-navigation) — symbol-id join across per-repository indexes.
- [SCIP — a better code indexing format than LSIF](https://sourcegraph.com/blog/announcing-scip) and [scip-code.org](https://scip-code.org/) — per-repository index, per-commit upload, incremental re-index.
- [Sourcegraph: writing an indexer](https://sourcegraph.com/docs/code-search/code-navigation/writing_an_indexer) — CI-produced index uploaded per commit.
- [gitrepository-layout](https://git-scm.com/docs/gitrepository-layout) and [git-worktree](https://git-scm.com/docs/git-worktree) — per-worktree vs common directory; `commondir`; a linked worktree's `.git` is a file.
- Local measurement, 2026-08-28: `mcp__codebase-memory-mcp__list_projects` (23 projects, 20 of them worktrees of one repository), `du -sh ~/.cache/codebase-memory-mcp` (1.2 GB), `resolve_repository_scope` on four directories, `collect_corpus` on `/home/user/agenticos/checkout-claude/main`.

## Related

- [[knowledge/notes/derived-evidence-generation-decision]] — generations are disposable derived cache; Markdown and Git remain authority.
- [[knowledge/notes/solo-operator-superset-product-decision]] — the contract clause this item serves.
- [[knowledge/notes/baseline-environment-binding-decision]] — identity bound to what is actually loaded, not to a whole-blob digest.
