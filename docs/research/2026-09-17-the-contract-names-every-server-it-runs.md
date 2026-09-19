# The contract names every server it runs

Dated 2026-09-17. Audit 3, finding K-D1. The research before the change.

## What was found

The contracts describe the precise tier as pinned Pyright and nothing else:

- `CLAUDE.md` / `AGENTS.md`, "Implemented code-navigation slice": "the
  production-quality, Python 3.10-compatible read-only LSP path through pinned Pyright
  1.1.411", "the process group covers pinned Pyright descendants only", "The managed Pyright
  artifact lives at `cache/code-tools/pyright/1.1.411/`".
- `docs/STRUCTURE.md` lines 95, 426, 458, 570; `docs/ARCHITECTURE.md`; `README.md` and its two
  translations: the same, Pyright only.
- `docs/CODE-NAVIGATION.md` is half-updated: its Languages bullet already lists four servers,
  while its summary, Trust, Installation and Process-ownership sections say Pyright.

The code registers four managed profiles (`scripts/lsp_profiles.py:681`,
`REGISTRY = ProfileRegistry((PYRIGHT_PROFILE, TYPESCRIPT_PROFILE, GOPLS_PROFILE,
RUST_ANALYZER_PROFILE))`) and `mcp_server._navigation_profile` routes a query to one of them by
file suffix, falling back to Pyright for an unclaimed suffix. Their pins and roots:

| profile | version | managed root |
|---|---|---|
| pyright | 1.1.411 | `cache/code-tools/pyright/1.1.411/` |
| typescript | 6.0.0 (tsserver 5.9.3) | `cache/code-tools/typescript-language-server/6.0.0/` |
| gopls | v0.23.0, built from pinned Go 1.27.1 | `cache/code-tools/gopls/v0.23.0/` |
| rust-analyzer | 1.98.1, with its pinned Rust toolchain | `cache/code-tools/rust-analyzer/1.98.1/` |

`scripts/install_pyright.py` installs the first; `scripts/install_language_server.py --profile
<name>` installs the other three — each still one explicit operator action, never a query.
`doctor.py`'s install hint names only Pyright. The containment statements ("Windows Job Object",
"POSIX process group", "hostile `setsid()` escape is unsupported") are written about Pyright,
but the mechanism in `lsp_process.py` is per assigned server and applies to all four.
`lsp_launch_package` seals a digest-verified copy of a native server inside
`run/lsp/<owner-nonce>/launch-*/` and launches it from there, which the contracts call only
"bounded process scratch".

## Practice on this date

- A contract document that is narrower than the running code is a defect of the same kind as a
  wrong comment: the reader plans against a surface that is not the one that exists. The vault's
  own rule (`CLAUDE.md` §0) requires the structure reference to be updated in the same change as
  the behaviour it describes; here the behaviour arrived (2026-09-11 and 2026-09-12 reports) and
  the contract text did not follow.
- The LSP specification is explicit that capabilities are per server: the `initialize` response
  returns an `InitializeResult` whose `capabilities` field holds "The capabilities the language
  server provides", and the specification introduces that structure with "The server can signal
  the following capabilities" (LSP 3.17 specification, Server lifecycle / `initialize`, fetched
  2026-09-17). A document that names one vendor therefore cannot describe a four-server tier —
  readiness and capability limits differ per server, which is exactly what the slice reports.

## The decision

Owner-delegated (audit item "решение владельца"). The contract text follows the code, without
changing the code:

- `CLAUDE.md` and `AGENTS.md` (kept byte-identical) name the four managed profiles, their pins,
  their roots and the two install commands, and state the containment guarantee per assigned
  managed server instead of per Pyright. The guarantees the slice already carries — read-only,
  no daemon, no new MCP tool, 12 tools, lazy start, structural fallback, install as a separate
  operator action — are unchanged.
- `docs/STRUCTURE.md`, `docs/ARCHITECTURE.md`, `docs/CODE-NAVIGATION.md` and the three READMEs
  say the same in their own register, and the sealed launch copy is named where the scratch
  directory is described.
- Doctor's install hint is left to the maintenance area (it is `doctor.py`, another agent's
  file in this round); it is named in the report instead.

Files: `CLAUDE.md`, `AGENTS.md`, `docs/STRUCTURE.md`, `docs/ARCHITECTURE.md`,
`docs/CODE-NAVIGATION.md`, `README.md`, `README.ru.md`, `README.zh-CN.md`,
`docs/research/2026-09-17-the-contract-names-every-server-it-runs.md`.
