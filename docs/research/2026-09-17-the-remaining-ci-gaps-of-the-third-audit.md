# The remaining CI gaps of the third audit

Dated 2026-09-17. Findings I-B2, I-C2, I-C3, I-C4 and I-C6 (CI gaps and one suspicion),
after I-C1 was closed by `docs/research/2026-09-17-a-real-install-is-run-in-ci.md`.

## What was found, and what was decided for each

### I-C2 — the hybrid job installs the extra and never uses it

`clean-hybrid` syncs `--extra hybrid` and then saves and loads a NumPy array. Neither
`sentence_transformers` nor any search runs, so a hybrid install that cannot search would
pass.

Decided: the job imports the package the extra exists for and then runs a real search
through `scripts/search_memory.py`. Model weights stay unfetched in CI — the read path is
local-only and says so: measured here on a fresh install, `search_memory.py "installation"`
exits 0 and prints `no dense signal — embedding model … is unavailable`. So the job proves
the install works and the degraded path is honest, without a model download.

### I-C3 — tests no job can ever run

Three dependencies are named by tests and installed by nothing: `jieba`
(`test_retrieval_v2_benchmark.py`), the managed TypeScript artifact
(`test_typescript_navigation.py`), and `torch` (`test_a_reranker_that_never_finished.py`,
one benchmark test).

Decided:
- `jieba` lives in the `lexical-benchmark` extra and its sdist is 19 MB: a small job syncs
  that extra with the dev group and runs the jieba tests.
- The TypeScript artifact is installed by an explicit operator action that downloads two
  pinned npm tarballs and verifies their Subresource Integrity hashes
  (`scripts/install_language_server.py`). A job does exactly that into a state root of its
  own, sets `LLM_WIKI_LSP_TEST_STATE_ROOT`, and runs the navigation tests, the way the
  Pyright job already installs Pyright explicitly.
- `torch` stays out of CI, and the workflow now says why: the locked torch pulls the CUDA
  stack (73 `nvidia-*` packages in `uv.lock`), several gigabytes for two test files, on
  every pull request. Fixing that means a CPU-only index pin in the lock — a dependency
  architecture change, not a CI change. Left named, not silently skipped.

### I-C4 — lint parses no PowerShell and runs no shellcheck

Decided: the `lint` job parses every tracked `.ps1` with the PowerShell parser (the same
`System.Management.Automation.Language.Parser` the installer tests use) and runs
`shellcheck` on `install.sh`. Measured here with shellcheck 0.11.0: one finding, SC2235
("Use `{ ..; }` instead of `(..)` to avoid subshell overhead") at the Python version guard;
rewritten, and the guard still answers `old` for 3.9. The owner's complexity gate is not
added: it is machine-local tooling under `~/.claude`, not part of this repository, and CI
cannot run it.

### I-C6 — a pre-commit ruff four hundred versions behind the lock

The hook pinned `ruff-pre-commit` at `v0.6.9` while `uv.lock` holds ruff 0.15.21, which is
what CI runs; the hook also covered only `scripts|tests` while CI lints `benchmark/` too;
and two "TODO: SHA-pin before next release" comments were open.

Decided: the ruff hook becomes a local hook running the project's own locked ruff over the
same three directories as CI, so there is one ruff version in the project and no third-party
rev to pin. The gitleaks hook is SHA-pinned: `v8.30.1` is commit
`83d9cd684c87d95d656c1458ef04895a7f1cbd8e` (read today from
`https://api.github.com/repos/gitleaks/gitleaks/git/ref/tags/v8.30.1`), with the version kept
in a comment. The local hooks pin their interpreter with `--locked --no-sync`, as every other
first-party invocation does.

### I-B2 — gitleaks scans a depth-1 clone

`actions/checkout` defaults to `fetch-depth: 1`, and the action scans a commit range.
Decided: `fetch-depth: 0` on that job only. The repository's history is 81 MB here, and the
job's budget is 10 minutes.

## Practice on this date

- `actions/checkout` documents `fetch-depth: 0` as "fetch all history for all branches and
  tags" (<https://github.com/actions/checkout>, the input's own description).
- shellcheck's own wiki page for SC2235 is the text quoted above
  (<https://www.shellcheck.net/wiki/SC2235>), printed by the run here.

Files: `.github/workflows/tests.yml`, `.pre-commit-config.yaml`, `install.sh`,
`tests/test_readme_i18n.py`,
`docs/research/2026-09-17-the-remaining-ci-gaps-of-the-third-audit.md`.
