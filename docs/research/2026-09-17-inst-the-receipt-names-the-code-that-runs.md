# The receipt names the code that runs, not only the file that loads it

Date: 2026-09-17. Audit 3, code intelligence, finding B20.

Files: `scripts/pyright_profile.py`, `scripts/install_pyright.py`,
`tests/test_the_receipt_names_the_code_that_runs.py`,
`tests/test_install_pyright.py`, `tests/test_pyright_profile.py`

## What was found

The install receipt records one content digest, `server_sha256`, over
`package/langserver.index.js`. Measured on the installed vault artifact
(`cache/code-tools/pyright/1.1.411/`, read-only, 2026-09-17) that file is 229
bytes and its whole body is:

```
global.__rootDirectory = __dirname + '/dist/';
require('./dist/pyright-langserver');
```

Everything the Node process actually executes lives under `package/dist/`:
`pyright-langserver.js` (2 008 B), `pyright-internal.js` (3 106 913 B),
`vendor.js` (747 981 B), `pyright.js` (2 033 B) and their source maps --
6 553 863 bytes in eight files -- plus `dist/typeshed-fallback/`, which brings
the package to 5 423 files and 19 278 695 bytes.

No digest covers any of it. The receipt is re-checked in three places and each
one checks only the shim:

* `pyright_profile._managed_manifest` -> `_executable_digest_codes`, on every
  `manager.get()` (discovery, the hot path);
* `install_pyright._verified_server_digest`, when an install loses the publish
  race and the existing tree is re-validated instead;
* `pyright_session`, before launch.

So a truncated, half-written or edited `dist/*.js` passes as the pinned
install, and the manifest attests itself.

## Cost, measured on the installed artifact

Rehashing the whole package tree: 5 418 files, 19 278 695 bytes, **1.29 s warm
and 4.17 s cold**. A `stat`-only walk of the same tree is **0.11--0.14 s**.
Hashing only the files directly under `package/dist/` -- the eight bundles the
`require` chain loads -- is **0.012--0.025 s** for 6 553 863 bytes.

Discovery runs on every `manager.get()` (audit finding B15), so neither the
1.3 s content walk nor the 0.13 s `stat` walk can go on that path. 20 ms can.

## Sources

- Node.js documentation, "Modules: CommonJS modules", `require(id)`
  (https://nodejs.org/api/modules.html, read 2026-09-17): a `require` of a
  relative path resolves to a file loaded and executed in the calling module's
  process. The shim therefore contributes 229 of the ~6.5 MB the server runs.
- The artifact itself, listed and hashed read-only at
  `cache/code-tools/pyright/1.1.411/` on 2026-09-17 (numbers above).
- This repository's own contract, `CLAUDE.md` -> "Implemented code-navigation
  slice": the managed Pyright artifact is pinned and installation is "a
  separate explicit operator action". Reinstalling is therefore a supported,
  cheap operator action, not a disruption.

## What this can and cannot promise

The manifest lives in the same operator-writable directory as the tree it
describes, and nothing signs it. Someone who can rewrite `dist/pyright.js` can
rewrite `executed_tree_sha256` next to it. The guarantee being added is
therefore **integrity, not authenticity**: a partial install, a truncated
write, a disk error, an interrupted extraction or a careless edit is caught.
That is exactly the guarantee `server_sha256` already gives for the shim, and
the defect is that it stops at 229 bytes.

## Alternatives

1. Rehash the whole package tree wherever the shim digest is checked today.
   Rejected on the measurement: 1.3--4.2 s on every `manager.get()`.
2. Record per-file sizes and compare by `stat` on the hot path. Rejected:
   0.13 s per query is still too much for the path B15 already calls too heavy,
   and it would not detect an equal-length change.
3. Rehash the full tree once per process, memoised. Rejected: it puts a
   1.3--4.2 s step inside whichever query happens to be first, under that
   caller's deadline, and it needs process-global state to be affordable.
4. Digest the files the `require` chain loads -- the shim plus every regular
   file directly under `package/dist/` -- and check that wherever the shim
   digest is checked today.

## Decision

Alternative 4.

* The receipt gains one field, `executed_tree_sha256`: SHA-256 over the sorted
  `"<relative posix path>\0<file sha256>\n"` lines of the shim and of every
  regular file directly under `package/dist/`. The installer computes it from
  the member digests it already takes while streaming the verified archive, so
  the value is bound to the pinned bytes and not to whatever is on disk
  afterwards.
* It is re-checked at discovery (`_managed_manifest`) and at lost-race
  re-validation (`_verified_server_digest`), bounded by file count
  (`MAX_EXECUTED_TREE_FILES = 64`), by bytes
  (`MAX_EXECUTED_TREE_BYTES = 32 MiB`) and by the caller's deadline through the
  same `_check_deadline` every other discovery step uses. A deadline that
  expires here fails the query exactly as it already does in `_server_digest`;
  it does not degrade the tier.
* Residual, stated rather than hidden: `dist/typeshed-fallback/` (5 410 stub
  files) is data Pyright reads, not code it executes, and is not covered. The
  measurement above is the reason. Closing it needs an explicit re-validation
  command, not the query path.

### An install written before this field

Decided: **refused by name, reinstall**. The manifest key set is closed
(`_manifest_shape_is_valid` compares `set(value)` with `_MANIFEST_KEYS`), so the
schema moves to `pyright-install/v2` and a nine-key receipt raises the dedicated
code `pyright_manifest_predates_tree_digest` instead of the generic
`pyright_manifest_malformed`. Pyright degrades to structural navigation and
doctor names the code.

Upgrading the old receipt in place was rejected twice over: it would record a
digest the installer never vouched for -- the self-attestation this finding is
about -- and it would write into the install root from the read-only discovery
path, outside the install lock. Reinstalling is one command and one 10 MB
download.
