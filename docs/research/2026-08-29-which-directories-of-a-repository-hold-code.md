---
type: raw-source
status: active
confidence: high
source_authority: web
created: 2026-08-29
---

# Which directories of a repository hold code

One-sentence summary: for a whole-repository index the answer that survives
contact with real repositories is *the set Git tracks*, filtered per file and
with every omission named — not a directory-name allowlist and not a build
manifest, both of which are partial by construction.

Dated research for the change that removes the last "this vault" assumption
from foreign-repository indexing: `corpus_snapshot.APPROVED_CODE_ROOTS`.

---

## 1. The question

`CODE-03` made the code index cover other repositories on this machine and
named its own remaining limit. `repository_index.selected_code_roots()`
discovers a repository's Git-tracked top-level directories, then refuses the
whole index unless every one of them is a name in
`corpus_snapshot.APPROVED_CODE_ROOTS` — `benchmark, docs, integrations, rules,
scripts, skills, tests`, which is *this vault's own layout*. A repository whose
code lives in `src/` is refused whole.

The refusal itself is correct and follows `NEW-67`: a partial index makes
"no result" indistinguishable from "not indexed", and for a memory system the
second failure is the worse one. What is wrong is the *predicate* — it asks
whether a foreign repository is shaped like us.

So: what should decide which directories of a foreign repository hold code?

## 2. What the comparable tools actually do

**Whole-repository search indexes take the version-control file set and filter
per file.** `zoekt-git-index` "works directly with Git repositories" and walks
the Git tree of the named branches (default `HEAD`); it does not take a
directory list. Its filesystem sibling `zoekt-index` walks a directory and
skips only `-ignore_dirs`, whose default is `.git,.hg,.svn`. The exclusions
that matter are per file, not per directory: files over `SizeMax` are recorded
with `SkipReasonTooLarge` rather than dropped. Sourcegraph's operator
documentation says the same from the outside — "Zoekt skips files that are
larger than 1 MB and binary files", raisable with `search.largeFiles`, and
crucially: to see which files were skipped, "visit the repository settings page
and click on indexing". The omissions are enumerable after the fact. That is
the same property `NEW-67` demands, arrived at from the other direction.

**Cursor uses Git's view of the repository as the file set**, respecting
`.gitignore` and adding `.cursorignore` for further exclusions — an ignore
mechanism layered on the tracked set, never a name allowlist.

**Precise per-language indexers use manifests, and are per-language.**
`scip-typescript` wants you to "navigate to the project root, containing
`tsconfig.json`", offers `--infer-tsconfig` when there is none, and needs
*separate* `--yarn-workspaces` and `--pnpm-workspaces` flags for monorepos.
`scip-python` says "ideally a `pyproject.toml`". Each answers "where is the
code" only for its own language, and each needs per-ecosystem maintenance.

**Where nothing can be inferred, everything falls back to explicit operator
configuration** — OpenGrok's `SRC_ROOT` is a directory the operator names, with
each subdirectory under it a project.

## 3. What a manifest would have said here, measured

Measured 2026-08-29 on the two real repositories on this machine.

`/home/user/agenticos/checkout-claude/main` tracks twelve top-level
directories (946 tracked files):

| root | tracked files |
|---|---|
| `src` | 360 |
| `tests` | 286 |
| `docs` | 80 |
| `skills` | 58 |
| `seed` | 43 |
| `vepkit` | 39 |
| `deploy` | 37 |
| `ops` | 25 |
| `scripts` | 13 |
| `fixtures` | 2 |
| `.github` | 2 |
| `config` | 1 |

Its `pyproject.toml` says:

- `[tool.hatch.build.targets.wheel] packages = ["src/agenticos"]` — **one** of
  the twelve.
- `[tool.hatch.build.targets.sdist] include = ["/src", "/tests", "/docs",
  "/scripts", ...]` — **four** of the twelve. `seed`, `vepkit`, `deploy`,
  `ops`, `config`, `fixtures` are absent.

A manifest reader would therefore index between 8% and 33% of that repository
and report success. That is precisely the `NEW-67` failure, arrived at
silently.

This vault is the harder case. `/home/user/llm-wiki/pyproject.toml` declares no
build backend and no `packages`, `py-modules` or `where` at all, so a packaging
reader learns *nothing* about our own layout. Reading tool sections instead
does not rescue it: `[tool.pyright] include = ["scripts", "tests",
"benchmark"]` and `[tool.pytest.ini_options] testpaths = ["tests"]`,
`pythonpath = ["scripts"]` between them name three of the seven roots the vault
actually indexes — `docs`, `integrations`, `rules` and `skills` are missed, and
`docs/` is where the decision and research pages that retrieval depends on
live.

The general form of this is documented rather than accidental: setuptools
auto-discovery "should cover most popular project directory organization
techniques, such as the src-layout and the flat-layout" when `packages` and
`py-modules` are left unspecified — so the common case is a manifest that
names nothing — and with flat-layout it deliberately refuses multiple top-level
packages "to prevent common errors such as accidentally publishing code not
meant for distribution (e.g. maintenance-related scripts)". A packaging
manifest describes what ships. A navigation index must cover what exists,
including the maintenance scripts packaging is designed to leave out.

## 4. The options, and what each costs

**(a) Git-tracked top-level directories.** Already implemented as
`repository_index.tracked_top_level_directories()`. Cost: requires Git, which
`admit_repository` already requires. Excludes build output, virtualenvs and
caches for free by honouring `.gitignore` — measured on agenticos, 946 tracked
files against a 2.2 GB checkout. Includes directories that are not code in any
narrow sense (`fixtures`, `config`, `seed`), which is a cost only if the
budget is tight; the collector already bounds files, bytes, entries and depth,
and refuses by name when a bound is hit. It says nothing about *which language*
anything is, which is correct: this index is language-agnostic.

**(b) A build or language-server manifest.** Cost: measured above — partial by
construction, and the omission is invisible. Adds a parser per ecosystem and a
maintenance obligation per ecosystem, and still leaves a repository's shell
`ops/` directory unclaimed by any manifest. Rejected.

**(c) An explicit `roots` argument.** Already implemented and kept. Zero
guessing and perfect provenance — the covered roots are recorded in the
generation's policy, so every later answer can say what was in scope. Cost: it
cannot be the default, because a product that replaces codebase-memory-mcp has
to answer "index this directory" without the operator first knowing the
repository's layout.

**(d) Combination — (a) as the default, (c) as the override.** Taken.

## 5. The one thing the tracked set does not settle

A tracked top-level directory can be one the corpus walk refuses to descend
into. `_Discovery._directory_excluded` prunes any name starting with `.`, plus
`SKIP_DIRECTORIES` and (outside historical mode) `ARCHIVE_DIRECTORIES`. Every
real repository tracks `.github`; agenticos tracks two files there.

Naming such a root as a code root is worse than skipping it. The walk does not
apply the prune rule to the root it is handed — only to children — so
`.claude` passed as a code root would be walked, and `.claude/worktrees/`
under it is a whole second copy of the repository. That is exactly the defect
closed in commit `1d06e6a`, where `_workspace_call_graph` walked 7,261 files
of 7,686.

So the rule is: **a directory the walk prunes is never a code root.** Not
because of its name in our layout, but because the collector already refuses
to look inside it, everywhere, including in this vault. Excluding it is not a
new gap — the product's answer for `.github/workflows/tests.yml` is already
"not indexed", here as well as there.

Silence is the part that has to be fixed, and Sourcegraph's "click on
indexing" is the precedent: the excluded names are carried in the index
receipt as `excluded_roots`, and the covered names are already recorded in the
generation policy and returned by `list_repositories`. An operator who wants
one of them anyway gets a named refusal rather than a surprise, because the
walk would collect nothing from it.

## 6. What this does not fix, named

- **Root-level tracked files are never indexed.** `README.md`,
  `pyproject.toml`, `bootstrap.sh`, `crontab.txt`, `env.template`,
  `onboarding.md`, `uv.lock` — seven non-hidden root files in agenticos, and
  the same seven names here. `tracked_top_level_directories()` only returns
  directories, and nothing else offers them. A code root *may* be a file
  (`_add_code_root` handles a regular path), so the fix is available; it is a
  different gap from the name allowlist and is not taken here.
- **`SKIP_DIRECTORIES` is still this vault's vocabulary.** `gaps` and
  `raw-sources` are knowledge-tree names, `_template` is ours, and they prune
  *inside* every code root of every repository. Measured: agenticos has no
  directory of any of those names under any root, so nothing is lost today.
  Fixing it means making the prune set depend on the walk kind, which changes
  what the vault's own generation contains, so it is not taken in the same
  change as this one.
- **The walk under a root is a filesystem walk, not a Git listing.** An
  untracked file under a tracked root is collected. This over-includes rather
  than under-includes, and the budgets bound it, but "indexed" and "tracked"
  are not the same set and the receipt does not say so.

## Sources

- [zoekt CLI indexing tools (DeepWiki)](https://deepwiki.com/sourcegraph/zoekt/4.3-cli-indexing-tools) — `zoekt-git-index` walks the Git tree of named branches; `zoekt-index` walks a directory skipping `-ignore_dirs` (default `.git,.hg,.svn`); `SizeMax` yields `SkipReasonTooLarge`.
- [sourcegraph/zoekt](https://github.com/sourcegraph/zoekt) — trigram index, one shard set per repository.
- [Search configuration — Sourcegraph docs](https://sourcegraph.com/docs/admin/search) — 1 MB and binary-file skips, `search.largeFiles`, and the per-repository "indexing" page listing skipped files.
- [scip-typescript](https://github.com/sourcegraph/scip-typescript) — project root containing `tsconfig.json`; `--infer-tsconfig`; separate `--yarn-workspaces` / `--pnpm-workspaces`.
- [scip-python](https://github.com/sourcegraph/scip-python) — `pyproject.toml` recommended.
- [Package Discovery and Namespace Packages — setuptools](https://setuptools.pypa.io/en/latest/userguide/package_discovery.html) — auto-discovery covers src-layout and flat-layout when `packages`/`py-modules` are unspecified; flat-layout refuses multiple top-level packages to avoid publishing maintenance scripts.
- [Ignore files — Cursor docs](https://cursor.com/help/customization/ignore-files) — indexing respects `.gitignore`; `.cursorignore` adds exclusions.
- [How to setup OpenGrok](https://github.com/oracle/opengrok/wiki/How-to-setup-OpenGrok) — operator-named `SRC_ROOT`, one project per subdirectory.
- Local measurement, 2026-08-29: `git ls-files` per top-level directory in `/home/user/agenticos/checkout-claude/main`; `pyproject.toml` of that repository and of this one.

## Related

- [[knowledge/notes/derived-evidence-generation-decision]] — generations are disposable derived cache; Markdown and Git remain authority.
- [[knowledge/notes/solo-operator-superset-product-decision]] — the contract clause this serves.
- `docs/research/2026-08-28-generations-for-other-repositories.md` — §6 named this hole; this note answers it.

---

## 7. Measured outcome, 2026-08-29

Indexed through `mcp_server._execute_tool_call("get_architecture", {"mode":
"index", ...})` against an isolated `LLM_WIKI_STATE_ROOT`; the live vault's
catalog was not touched.

`/home/user/agenticos/checkout-claude/main`, eleven code roots discovered
(`config, deploy, docs, fixtures, ops, scripts, seed, skills, src, tests,
vepkit`), `.github` reported in `excluded_roots`:

| | |
|---|---|
| sources | 943 |
| chunks | 1,781 |
| index time | 494.6 s |
| generation on disk | 176,146,026 bytes (168 MiB) |
| vector state | complete |

943 against 944 tracked files under those roots is fully accounted for:
`skills/theme-factory/theme-showcase.pdf` does not decode as UTF-8, so the
collector hashes it and leaves it out of the corpus, which is the existing
`_decodes_as_utf8` contract.

Navigation against `src/`, a directory that could not be indexed at all before
this change: `mode=callers, symbol=_claim_key` returned three callers, all in
`src/agenticos/assurance.py` at lines 731, 772 and 787, which is exactly what
`grep -rn "_claim_key(" --include=*.py` returns.

The refusals still fire by name, through the tool path, on that same real
repository: `roots=[".github"]` and `roots=["src", ".github"]` both give
`repository_root_not_collectable` naming `.github` and refusing the *whole*
request rather than quietly narrowing it to `src`; `roots=["nowhere"]` gives
`repository_root_missing`. A constructed repository tracking nothing but
`.github` gives `repository_has_no_code_roots` and names the pruned directory.

The vault's own corpus is byte-identical across the change. Collected on this
checkout's non-moving code roots (`integrations, rules, scripts, skills` --
`benchmark`, `docs` and `tests` were being written by other agents during the
measurement), old code and new code alternately, four runs: 442 sources and
SHA-256 `e61e1cd15a73e075…` of the membership-plus-hash map, every time.

## 8. Residuals found while measuring

- `list_repositories` reports a generation's `code_roots` but not its
  `excluded_roots`: the generation manifest's policy has no field for them, and
  adding one changes the corpus policy schema, which invalidates every
  published generation (`NEW-81`). A reader can still derive it --
  `tracked_top_level_directories(root)` minus the recorded `code_roots` -- but
  the listing does not say it.
- The foreign graph reports `graph_complete: false` with
  `unresolved_count: 58181` on that repository. That is the extractor's
  cross-file resolution, not root selection, and it is unchanged by this work.
