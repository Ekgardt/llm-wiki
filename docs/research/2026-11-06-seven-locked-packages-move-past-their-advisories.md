# Seven locked packages move past their advisories

Date: 2026-11-06.

## What was true

The `dependency-audit` job (docs/research/2026-09-27-the-lock-is-audited.md) failed
for every pull request, including ones that touch no dependency: run on the lock at
a5c67aee, its three steps report "Found 51 known vulnerabilities in 7 packages" and
exit 1. None of the seven is capped by `pyproject.toml`; each was held only by the
lock's earlier resolution.

| Package | Locked | Fixed in | Arrives through |
|---|---|---|---|
| certifi | 2026.6.17 | 2026.10.18 | httpx/httpcore (mcp, huggingface-hub), requests (`audit` group) |
| filelock | 3.28.0 | 4.2.1 | huggingface-hub, torch (`semantic`, `reranker`, `retrieval-benchmark`), virtualenv and python-discovery (`dev`), cachecontrol (`audit`) |
| mcp | 1.29.0 | 1.30.0 / 2.2.0 | direct, `mcp>=1.29,<2` |
| msgpack | 1.2.2 | 1.2.3 | cachecontrol (`audit`) |
| pyjwt | 2.13.0 | 2.14.0 | `mcp[crypto]` |
| setuptools | 83.0.0 | 84.1.0 | torch (`reranker`, `retrieval-benchmark`) |
| virtualenv | 21.2.4 | 21.7.12 | pre-commit (`dev`) |

The repository's own code imports only `mcp` (`scripts/mcp_server.py`,
`scripts/mcp_http.py`, `scripts/install_smoke.py`); the other six are carried by
other packages.

## Sources

1. PyPI JSON API, `https://pypi.org/pypi/<name>/<version>/json`, field
   `vulnerabilities` (read 2026-11-06) -- the feed pip-audit queries by default.
   It names the advisories and fixed versions above, and none for the new versions.
2. MCP Python SDK v1.30.0 release notes,
   https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.30.0
   (published 2026-09-07, read 2026-11-06): "Idle Streamable HTTP sessions now
   expire ... A stateful session with nothing in flight for 30 minutes is closed.
   The client's next request gets a 404 and it has to initialize again ... A server
   also holds at most 10 000 sessions at once; beyond that, new sessions get a 503."
3. filelock release notes 4.0.0 to 4.2.1,
   https://github.com/tox-dev/filelock/releases (read 2026-11-06): the only
   breaking change of 4.0.0 is the on-disk protocol of `SoftReadWriteLock`; 4.2.1
   is the security release. PyPI metadata: 4.1.1 and later require Python >= 3.11.
4. virtualenv 21.15.5 metadata on PyPI (read 2026-11-06): requires
   `filelock>=3.24.2,<5`, `packaging>=26.3`, `distlib>=0.4.3,<1`,
   `python-discovery>=1.6.1`.

## Alternatives

- `mcp` 2.x (2.2.0 also fixes the advisories): a new major line with its own API;
  out of scope for this change, so `mcp>=1.29,<2` stays and 1.30.0 is taken.
- Ignoring advisories in the job (`--ignore-vuln`): weakens the check (law 6).
- `uv lock --upgrade-package` for the seven names only (chosen): no constraint in
  `pyproject.toml` changes.

## Verified

- `uv lock --upgrade-package` for the seven names, with the pinned uv 0.12.3,
  moved certifi to 2026.10.23, mcp to 1.30.0, msgpack to 1.2.4, pyjwt to 2.15.1,
  setuptools to 84.2.1, virtualenv to 21.15.5, and filelock to 4.2.9 on Python
  3.11+ and 4.1.0 on Python 3.10. Three packages moved because virtualenv 21.15.5
  requires them (source 4): distlib 0.4.0 to 0.4.4, packaging 24.2 to 26.3 and
  python-discovery 1.2.2 to 1.9.4. Nothing else changed.
- The job's three steps on the new lock: "No known vulnerabilities found", exit 0.
- The MCP, HTTP, install-smoke and CI-policy tests pass on the new lock (478 passed,
  2 skipped). The full suite on Linux, Python 3.12: 10 068 passed, 219 skipped,
  2 failed; both failures are the sandbox the run had, not the lock
  (`systemd-analyze` on a read-only filesystem, and `socket(AF_UNIX)` refused with
  EPERM). Python 3.10, 3.11, 3.13, 3.14, Windows and macOS were left to CI.

## Trade-offs

- Python 3.10 keeps a vulnerable filelock. Every filelock release that fixes the
  advisories (4.2.1 and later) requires Python >= 3.11, so the lock resolves 4.1.0
  for Python 3.10; pip-audit lists 17 advisories against it. The job does not see
  them: it runs on the runner's Python 3.12 and pip-audit skips a requirement whose
  marker (`python_full_version < '3.11'`) does not match the running interpreter.
  Auditing the marker-restricted pins separately finds this one package and nothing
  else. The 3.10 line was already exposed before this change (3.28.0, nine
  advisories); it cannot be fixed while the product supports Python 3.10. On 3.10,
  filelock arrives only with the `semantic`, `hybrid`, `reranker`, `full` and
  `retrieval-benchmark` extras and the `dev` and `audit` groups, not the base install.
- The HTTP MCP server (`scripts/mcp_http.py`) builds a stateful
  `StreamableHTTPSessionManager` with the SDK defaults, so it now inherits
  `session_idle_timeout=1800` and `max_sessions=10000` (source 2). A client idle
  for 30 minutes without an open GET stream re-initializes after a 404; that expiry
  is the fix for GHSA-84m7-p3x7-pcfv, which let sessions accumulate without bound.
