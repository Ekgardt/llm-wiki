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
- Ignoring advisories for every interpreter (`--ignore-vuln` in the one job):
  would hide them on 3.11+, where a fix is locked (law 6). Not taken.
- Dropping Python 3.10: a product contract change (`requires-python`, the
  installers, the 3.10 CI legs), not a dependency update. Not taken here.
- `uv lock --upgrade-package` for the seven names only (chosen), plus one
  constraint for the one package whose fix cannot reach Python 3.10.

## Python 3.10 cannot have a fixed filelock

Every filelock release that fixes the advisories (4.2.1 and later) requires Python
>= 3.11 (source 3), so the seven upgrades alone resolved 4.1.0 for Python 3.10. The
PyPI feed (source 1) counts the advisories per 3.10-capable release:

| filelock | Advisories |
|---|---|
| 3.24.2 to 3.29.7 | 9 (the same nine as 3.28.0, the version on `main`) |
| 3.30.0 to 3.32.5 | 12 |
| 3.32.6, 3.32.7 | 13 |
| 4.0.0 to 4.0.8 | 14 |
| 4.0.9 to 4.0.12 | 13 (fixes one, adds others) |
| 4.1.0 | 17 |

`[tool.uv] constraint-dependencies = ["filelock<3.30; python_full_version < '3.11'"]`
keeps 3.10 on 3.29.7: the fewest advisories any 3.10-capable release has, and no
more than before this change. It lapses when 3.10 support ends or filelock
publishes a fix that installs on 3.10. On 3.10, filelock arrives only with the
`semantic`, `hybrid`, `reranker`, `full` and `retrieval-benchmark` extras and the
`dev` and `audit` groups, not with the base install.

## The audit reads every pin

pip-audit evaluates each exported requirement's environment marker against the
interpreter it runs on and silently skips the requirements that do not match. The
job ran once, on Python 3.12 on Linux, so it never read the Python 3.10 and 3.11
forks of the lock (filelock, numpy, scipy, onnxruntime, networkx, scikit-learn and
others) nor the Windows-only pins (colorama 0.4.6, pyreadline3 3.5.6, pywin32 312).
That is how the first version of this change passed while 3.10 held 17 advisories.

The job is now a matrix: one leg for each supported Python (3.10 to 3.14) on
Linux, and Python 3.10 on Windows. Each leg pins its interpreter, exports the whole
lock and audits it. `tests/test_the_audit_reads_every_locked_pin.py` walks `uv.lock`
the way an install does, once per leg, and fails unless the legs together install
every locked version; macOS adds no pin today. The nine filelock advisories are
accepted by GHSA ID on the two Python 3.10 legs only, through a YAML anchor in the
matrix; the test refuses an acceptance on any other Python, and refuses one at all
once `requires-python` no longer admits 3.10.

## Verified

- `uv lock --upgrade-package` for the seven names, with the pinned uv 0.12.3 and
  the constraint above, moved certifi to 2026.10.23, mcp to 1.30.0, msgpack to
  1.2.4, pyjwt to 2.15.1, setuptools to 84.2.1, virtualenv to 21.15.5, and filelock
  to 4.2.9 on Python 3.11+ and 3.29.7 on Python 3.10. Three packages moved because
  virtualenv 21.15.5 requires them (source 4): distlib 0.4.0 to 0.4.4, packaging
  24.2 to 26.3 and python-discovery 1.2.2 to 1.9.4. Nothing else changed.
- The job's steps on Python 3.12: "No known vulnerabilities found", exit 0.
- The fifteen pins a Python 3.12 run cannot see, audited directly: only filelock
  3.29.7 has findings (nine); with the nine accepted IDs, "No known
  vulnerabilities found, 9 ignored".
- The coverage test fails when the matrix is reduced to the one Python 3.12 leg the
  job used to have, and names those fifteen pins.
- The test results of this change are listed in its pull request; Python 3.10,
  3.11, 3.13, 3.14, Windows and macOS legs run in CI.

Update 2026-11-09. The per-interpreter matrix below and
`tests/test_the_audit_reads_every_locked_pin.py` were retired with the move to
Python 3.14 as the only interpreter; see
`docs/research/2026-11-03-all-marker-dependency-audit.md`. No Python 3.10 fork and no
accepted filelock advisory is left. The lock now holds filelock 4.2.9, virtualenv
21.15.6 and python-discovery 1.9.5.

## Trade-offs

- Python 3.10 keeps filelock 3.29.7 with nine known advisories, as `main` did with
  3.28.0. The audit now says so by name instead of not seeing it.
- The audit takes six runners instead of one.
- The HTTP MCP server (`scripts/mcp_http.py`) builds a stateful
  `StreamableHTTPSessionManager` with the SDK defaults, so it now inherits
  `session_idle_timeout=1800` and `max_sessions=10000` (source 2). A client idle
  for 30 minutes without an open GET stream re-initializes after a 404; that expiry
  is the fix for GHSA-84m7-p3x7-pcfv, which let sessions accumulate without bound.
  Passing the two values explicitly, as settings, needs the floor raised to
  `mcp>=1.30`: 1.29.0 accepts `session_idle_timeout` (default: no expiry) but has no
  `max_sessions` argument (read from its wheel on 2026-11-06). That also changes the
  installer's dependency contract (`scripts/sync_memory.py`) and its tests, and is
  left as a follow-up.
