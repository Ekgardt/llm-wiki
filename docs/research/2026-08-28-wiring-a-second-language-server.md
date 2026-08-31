# Wiring a second language server into the working path

Date: 2026-08-28.
Question: `CODE-08` landed `scripts/lsp_profiles.py` and
`scripts/lsp_server_profile.py` as data with 60 passing tests and *no importer*
— the commit message says so itself. This note settles the three design
questions that stand between that seam and a `get_architecture` call that
answers a TypeScript question: how the facade picks a server for a file, what it
does when that server is not installed, and why neither choice can move Python.

Everything marked "measured" was run on this machine today against
`node v22.23.2`. The predecessor note
(`docs/research/2026-08-28-precise-navigation-beyond-python.md`) established the
pins, the readiness finding and the language boundary; this note does not repeat
them, it builds on them.

## Re-verification of the two pins before building on them

A pin recorded yesterday is not a pin verified today. Both tarballs were fetched
from the registry and hashed again:

```
typescript-language-server-6.0.0.tgz   515,598 bytes
  sha256 6e23b48efc76af4e70928cdfe62ea6e6cfef67ab4c1e7579c4e82dd284fbdfd2
  SRI    sha512-LXtzY3UZGfghWA5eRU6/T5j1+YiGRgy14mR3GOKyTKlE1op1TYKQnLVxwBsmnXeDhGLuvzZyIHBAqvrekAITYQ==
typescript-5.9.3.tgz                 4,377,468 bytes
  sha256 10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3
  SRI    sha512-jl1vZzPDinLr9eUt3J/t7V6FgNEw9QjvBPdysz9KfQDD41fQrC2Y4vKQdiaUpFT4bXlb1RHhLpp8wtm6M5TgSw==
```

Both match `TYPESCRIPT_PACKAGE_SHA256` / `TYPESCRIPT_PACKAGE_INTEGRITY` and
`TSSERVER_PACKAGE_SHA256` / `TSSERVER_PACKAGE_INTEGRITY` in `lsp_profiles.py`
byte for byte. `node -v` is `v22.23.2`, which clears both
`TYPESCRIPT_NODE_MAJOR = 22` and `TYPESCRIPT_NODE_MINOR_FLOOR = 22`.

## Question 1 — how the facade chooses a server for a file

**Current practice.** Every multi-language LSP client surveyed decides this the
same way, and it is not a clever decision: a static table from file identity to
language id, and from language id to a server. VS Code's extension guide uses a
`documentSelector` of language ids; the LSP specification's own document filter
"denotes a document through properties like `language`, `scheme` or `pattern`";
Sublime's LSP configures each client with `languageId`, `scopes` and `syntaxes`;
`lsp-mode` maps a major mode to a language id with
`lsp-language-id-configuration` and then registers a client against it;
`kakoune-lsp` maps filetype to `lsp_language_id` and permits more than one
server per filetype. There is no dynamic negotiation anywhere. Content sniffing
appears nowhere.

**Decision.** Route by file suffix, through the registry the seam already
carries — `ProfileRegistry.for_path`, backed by `LanguageServerProfile.file_suffixes`.
`ProfileRegistry.__init__` already refuses a suffix claimed by two profiles, so
the table is a function by construction rather than by convention, and the
"more than one server per filetype" case that `kakoune-lsp` permits is
deliberately *not* taken: two servers answering one file means two answers to
reconcile, and this product's contract is one precise provider or the structural
tier.

Rejected: routing by the graph's detected language. It would introduce a second
authority for the same question and make the choice depend on whether the
corpus generation is fresh. Rejected: content sniffing. Nobody does it, and a
`.ts` file that fails to parse is still a TypeScript file.

The unresolved-suffix case is not an error and must not be reported as one: a
repository full of Go, Rust and Markdown is the normal case, `for_path` returns
`None`, and the request takes the structural-evidence path it takes today. The
registry's own docstring already says this; the wiring must honour it.

## Question 2 — what happens when the server for that language is not installed

The accepted decision
(`knowledge/notes/read-only-lsp-navigation-engine-decision.md`) fixes two things
that together answer this: installation is **a separate explicit operator
action**, and an unavailable precise tier **falls back to existing structural
evidence** while **reporting readiness and capability limits**.

So absence is a *normal steady state*, not a failure, and the three things it
must not do are: install anything, raise, or answer from a server that is not
the pinned one.

**Decision.** Discovery for a non-Pyright profile looks in exactly one place —
the managed root the profile names — and nowhere else. If the entry script is
absent, discovery returns a `missing` identity carrying one degradation code in
the profile's own namespace (`typescript_missing`), the session is never
started, and the query falls through to structural candidates with the code
carried into the envelope's warnings beside `provider.name = "typescript"`.

Rejected: falling back to a `typescript-language-server` on `PATH`, the way
Pyright discovery accepts a `system` candidate. Pyright's system path is
defensible because that discovery re-derives the full identity — package
version, executable digest, lockfile evidence — and degrades loudly when it
does not match the pin. For TypeScript the analogous check is weaker in a way
that matters: the engine is resolved at *runtime* from `initializationOptions`,
so a system server would still be handed our pinned `tsserver.js`, and the
combination "unpinned server, pinned engine" is a configuration nobody
measured. One installed shape, or none.

Rejected: installing on demand. It is the one thing the accepted decision names
as an operator action, and a first navigation query is the worst possible moment
to download 4.9 MB.

## Question 3 — why this cannot move Python

Three mechanisms, in increasing order of how much I trust them:

1. **The Pyright profile is derived, not retyped.** `PYRIGHT_PROFILE` imports
   `PYRIGHT_CONFIGURATION`, `PYRIGHT_INITIALIZATION_OPTIONS`,
   `PYRIGHT_SERVER_RELATIVE`, `PYRIGHT_PACKAGE_URL`,
   `PYRIGHT_PACKAGE_INTEGRITY` and `QUALIFIED_NODE_MAJOR` from
   `pyright_profile.py`. `tests/test_lsp_server_profile.py` already asserts the
   argv, the configuration bytes and the initialization-option bytes are
   identical to what the Pyright path built before the seam existed. Changing
   Pyright's behaviour through the profile would require editing the constants
   the existing Pyright tests pin.
2. **The profile defaults to Pyright at every new seam.** Every parameter added
   in this change (`profile=` on the session, the manager, the facade) defaults
   to `PYRIGHT_PROFILE`, so a caller that does not know about profiles gets
   exactly the object graph it got before.
3. **The two behaviours that are genuinely new are gated on profile fields that
   Pyright does not set.** The readiness gate fires only when
   `profile.gates_on_progress()` — `READINESS_WORK_DONE_PROGRESS`, which
   `PYRIGHT_PROFILE` does not use. The post-initialize identity assertion fires
   only when `profile.identity_notification is not None` — `PYRIGHT_PROFILE`
   sets it to `None`, and the predecessor note explains why: Pyright's identity
   is the digest of the file we launch, settled before the process starts.

The claim to *test*, not to argue, is the qualification gate
`warm_overhead_p95_ms = 30`. The readiness gate costs 0.67–0.82 s and is a
**cold** cost paid once per project load; it must not appear in the warm path.
That is checked by running the Pyright suites and the benchmark unchanged.

## The readiness gate, and why it is not optional

The specification is explicit that this signal is opt-in and that the client
must ask for it:

> "To keep the protocol backwards compatible servers are only allowed to use
> `window/workDoneProgress/create` request if the client signals corresponding
> support using the client capability `window.workDoneProgress`."

Measured yesterday: ungated, `typescript-language-server` answers
go-to-definition with the *import binding* rather than the declaration — 0/12
correct across 12 fresh processes; gated on the `$/progress` `end` that closes
the token the server created, 12/12. The answer is well-formed and wrong, so no
caller can detect it downstream. A wired path without the gate is worse than no
wired path.

Measured today, before writing any wiring: `pyright_session._CLIENT_CAPABILITIES`
**already** declares `"window": {"workDoneProgress": True}` and
`_server_request_handlers` **already** answers `window/workDoneProgress/create`
(with `_benign_server_request`, which replies `null` — the correct reply).
`$/progress` is already in `lsp_protocol.SERVER_NOTIFICATIONS` and already
retained by `_progress`. So the transport half of the gate exists and needed
nothing. What is missing is only the *policy*: nothing anywhere treats the
arrival of an `end` as a precondition for answering.

**Decision.** Put the gate at `_document_ready_locked`, not at
`_refresh_readiness_locked`. Measured by reading the call graph:
`_refresh_readiness_locked` feeds the reported `readiness` string, while a query
is admitted through `_document_query` → `_query_ready_locked` →
`_document_query_current_locked` → `_document_ready_locked`. Gating only the
first would have produced a session that *reports* itself not ready and answers
anyway — the precise failure this gate exists to prevent. `_document_ready_locked`
is the single point both paths pass through.

The waiting is done in `_begin_document_query`, after `open_document`, bounded
by the caller's deadline, on the existing `self._condition`. The `$/progress`
handler already takes `self._lock` (in `_retain_progress`), so notifying there
adds no new lock order.

## What a profile-driven session must not become

The predecessor note measured 171 Pyright-mentioning lines inside 5,028, and
rejected copying the module. This note adds the symmetric rejection: do **not**
extract a `LanguageServerSession` base class with a `PyrightSession` subclass.
The 4,857 neutral lines are neutral because they are *one* implementation of
containment, leases, generations and wire state; a base/subclass split invites a
second override of exactly those, which is the same duplication with better
manners. The seam is a *field*, not a subclass: one class, one `profile`
attribute, and every language-shaped constant read from it.

The class is renamed `LanguageServerSession` with `PyrightSession` kept as an
alias, because a class named `PyrightSession` driving `typescript-language-server`
is a lie in the source, and because `code_navigation.CodeNavigation` type-checks
the session by that name.

Honest residue, named rather than hidden:

- `PyrightIdentity` is reused for every profile. Every field on it is
  language-neutral (status, source, version, node executable, digests,
  qualification, degradation codes) but its *name* is not. It lives in
  `scripts/pyright_profile.py`, which this task is not permitted to touch, so
  renaming it is left as a follow-up rather than done badly.
- `lsp_protocol.SERVER_NOTIFICATIONS` stays a module-level allowlist rather
  than a per-session set. Threading a set through `lsp_process` (6,656 lines,
  zero Pyright mentions) to gain a per-profile transport allowlist would put
  language into the one layer the measurement found free of it. Instead the
  allowlist becomes the *union over the registry*, computed from it, so there
  is still one source of truth and a test can assert the union.

## Sources

- [LSP 3.17 specification — window/workDoneProgress/create and $/progress](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/)
- [VS Code Language Server Extension Guide (documentSelector)](https://code.visualstudio.com/api/language-extensions/language-server-extension-guide)
- [Sublime Text LSP documentation (languageId / selector)](https://lsp.readthedocs.io/en/stable/)
- [lsp-mode — adding a new language](https://emacs-lsp.github.io/lsp-mode/page/adding-new-language/)
- [kakoune-lsp (lsp_language_id, several servers per filetype)](https://github.com/kakoune-lsp/kakoune-lsp)
- [typescript-language-server on npm](https://www.npmjs.com/package/typescript-language-server)
- `docs/research/2026-08-28-precise-navigation-beyond-python.md`

## Measured after wiring (2026-08-29)

The design above is implemented and routed. What it does **not** yet produce is a
correct TypeScript answer, and the two reasons are measured rather than guessed.
Both are in modules the design did not expect to touch, and both are the same
kind of thing: a Pyright-shaped assumption in a layer the earlier measurement
counted as neutral.

**What works, measured end to end through `mcp_server._execute_tool_call`:**

```
PYTHON  definition   provider pyright/1.1.411   query_ready  lsp_confirmed  1 hit   0.78 s
PYTHON  references   provider pyright/1.1.411   query_ready  lsp_confirmed  4 hits  0.03 s
TS      (missing)    provider typescript/None   not_ready    warnings: [..., "typescript_missing"]
```

Routing, per-language session keying, discovery, the install receipt, the
degradation namespace and the capability-limit channel all behave as designed.
The explicit installer fetched both tarballs and both matched their pinned SRI
byte for byte.

**Blocker 1 — the launcher is CommonJS-shaped, in two independent ways.**
`_LaunchServerGuard._posix_launch` copies the server aside into the owner scratch
root, verifies the copy's digest, and launches it through `_NODE_MAIN_LOADER`,
which does `Module._compile(source, filename)`. Measured against the installed
`package/lib/cli.mjs`:

```
node <snapshot-copy> --stdio    -> Error: ENOENT ... open '/package.json'
Module._compile(<cli.mjs>)      -> ERR_REQUIRE_ASYNC_MODULE
```

The second is the obvious one: `cli.mjs` is an ES module (`"type": "module"`,
`import` on line 2) and cannot be compiled as CommonJS. The first is the
interesting one and survives fixing the second: the server reads `package.json`
*relative to its own location*, and the launch strategy deliberately runs it from
a copy somewhere else. Pyright's `langserver.index.js` does not, which is why
this never surfaced. The observable symptom is
`ProtocolViolation: unexpected EOF in LSP header` and
`typescript_startup_failed`.

This is not a bug to patch in passing. The copy-aside-and-launch-from-a-descriptor
strategy exists to close a TOCTOU window on the server file, and any fix trades
against that guarantee. It needs its own dated research and its own decision
page.

**Blocker 2 — the freshness contract only knows Python.**
`workspace_revision._is_relevant_path` is `suffix in {".py", ".pyi"} or
_is_configuration(path)`. Measured on the TypeScript fixture,
`compute_workspace_revision(...).entries == []`, so `_load_revision_document`
cannot validate `src/main.ts` and the answer is `status: "error"`,
`"source document validation failed"` — before the server matters at all. The
fix is to read the suffix set from the profile registry, the same way the routing
does.

**Why neither was fixed here.** `scripts/workspace_revision.py`,
`scripts/code_navigation.py` and `scripts/lsp_protocol.py` are each refused
*wholesale* by the complexity gate over pre-existing findings (39 in
`code_navigation.py`, roughly 30 in `lsp_protocol.py`), so a one-line change to
any of them is a several-hundred-line refactor of a safety-critical module first.
`scripts/pyright_session.py` was in the same state -- 49 findings -- and was
cleaned to zero as part of this work, because it could not be avoided.

That is the finding worth carrying forward: what stands between this seam and a
working second language is not the seam. It is that three of the four modules a
second language has to touch cannot currently be edited at all.
