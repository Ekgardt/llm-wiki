# The lock is audited

Date: 2026-09-26 (audit of 2026-09-27, item B-19).

Update 2026-09-29: the PyPI pass skips the vendor's local `+cpu` build. CI now
retains that pass and adds a strict OSV pass over the same export. See
`docs/research/2026-09-29-a-local-build-is-not-an-audit-skip.md` for the reproduced
gap, exact-version audit and known-vulnerable positive control. The original
choice below remains the historical record.

## What was true

`uv.lock` pinned anyio 4.14.1 and cryptography 49.0.0. OSV (queried
2026-09-26, `https://api.osv.dev/v1/query`) lists GHSA-3w57-8xmc-8v26,
GHSA-5p39-cfhj-2xmp and GHSA-82r6-8w77-94w6 against anyio 4.14.1, fixed in
4.14.2, and GHSA-g6cj-pr64-35w5 / PYSEC-2026-3552 against cryptography 49.0.0,
fixed in 50.0.0. anyio arrives through mcp, httpx, starlette and sse-starlette;
cryptography through `pyjwt[crypto]`. Nothing in CI read the lock against
advisories, so the lock could hold them indefinitely.

## Sources

1. pip-audit README, https://github.com/pypa/pip-audit (read 2026-09-26):
   "Audits Python environments, requirements files and dependency trees for
   known security vulnerabilities"; it uses "the Python Packaging Advisory
   Database via the PyPI JSON API" and supports OSV; `--requirement` with
   `--disable-pip` audits a hashed requirements file without resolving it again.
   Latest release v2.10.1, 2026-06-10.
2. GitHub Docs, "Security hardening for GitHub Actions",
   https://docs.github.com/en/actions/reference/security/secure-use (read
   2026-09-26): "Pinning an action to a full-length commit SHA is currently the
   only way to use an action as an immutable release."
3. pypa/gh-action-pip-audit, `requirements.txt` at commit
   fb241f581674a1bb995061d62504857a9ea4b69e (main, 2026-06-08): the action
   installs `pip-audit ~= 2.0, >= 2.5.6` -- pinning the action by SHA does not
   pin the auditor it installs. Latest tagged release v1.1.0 is from 2024-08-08.
4. OSV-Scanner, supported lockfiles,
   https://google.github.io/osv-scanner/supported-languages-and-lockfiles/
   (read 2026-09-26): "Python | `Pipfile.lock` `poetry.lock` `requirements.txt`
   `pdm.lock` `pylock.toml` `uv.lock`".

## Alternatives

- gh-action-pip-audit pinned by SHA: the action is immutable, the pip-audit it
  installs floats (source 3), so a run is not reproducible.
- google/osv-scanner-action: reads `uv.lock` directly (source 4) but runs a
  container image and a second advisory client; one more external action to pin.
- Dependabot alerts: a repository setting the owner controls, not a check that
  fails a pull request.
- pip-audit from the lock (chosen): a separate `audit` dependency group, not in
  the default groups, so no user or test environment carries it. CI runs
  `uv sync --locked --only-group audit`, so the auditor's version and hashes
  are the lock's own, then exports every extra and group with hashes and runs
  `pip-audit --requirement ... --disable-pip`. No new action; the check uses the
  already SHA-pinned checkout and setup-uv.

## Verified

- On the old lock the three steps report "Found 5 known vulnerabilities in 2
  packages" and exit 1; on the new lock "No known vulnerabilities found", exit 0.
- `uv lock --upgrade-package anyio --upgrade-package cryptography` moved anyio to
  4.15.1 and cryptography to 50.0.1 (typing-extensions 4.15.0 to 4.16.0 came
  with them); OSV lists no advisory for either new version. The MCP, HTTP,
  security and compile tests pass on them (479 passed, 1 skipped).

## Trade-offs

- The job needs network and reads advisories published after a commit, so a
  new advisory can turn `all-green` red on unchanged code. That is the purpose:
  the fix is a lock upgrade, as here.
- pip-audit reads the PyPI advisory feed; an advisory published only in another
  database is not seen.
