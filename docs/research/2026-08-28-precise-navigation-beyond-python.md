# Precise navigation beyond Python

Date: 2026-08-28.
Question: `CODE-08` asks for precise navigation beyond Python, "TypeScript next,
by the same contract as the Pyright path". Which TypeScript server can be pinned
and installed offline as a managed artifact under `cache/code-tools/`, how does
its initialization differ from Pyright's, and what does it need from a repository
that this vault cannot assume exists?

Everything below marked "measured" was run on this machine today against
`node v22.23.2`. Fixture and probe scripts lived under `/tmp/cd08/`; nothing was
written into `knowledge/`, `run/`, or `cache/`.

## Finding 1 — the obvious pin is the wrong one: TypeScript 7 has no tsserver

`typescript@latest` is `7.0.2`. Pinning `latest` would have been the default
move and it does not work, for a reason that is invisible from the version
number.

Measured, by downloading the tarball and listing it:

```
TS 7.0.2  package/lib/  ->  version.cjs  version.d.cts  getExePath.js  tsc.js
          grep -c tsserver over the member list -> 0
          package.json bin -> {"tsc": "./bin/tsc"}
TS 5.9.3  package/lib/  ->  tsserver.js  _tsserver.js  tsserverlibrary.js ...
```

TypeScript 7 is the Go-native port. Its `lib/` is a shim: `getExePath.js`
locates a native executable that ships in a *separate, per-platform* npm package
named in `optionalDependencies` — twenty of them, `@typescript/typescript-linux-x64`
through `@typescript/typescript-sunos-x64`. Measured: `@typescript/typescript-linux-x64@7.0.2`
is 27,945,990 unpacked bytes across 114 files, with its own integrity hash.

Microsoft's own announcement and the `typescript-language-server` README both
say the intended successor is TS 7's native `tsgo --lsp`, which speaks LSP
directly and needs no Node wrapper. That is the right long-term target and it is
not the right pin today, for a contract reason rather than a taste reason. The
Pyright contract pins **one** URL and **one** Subresource Integrity hash
(`PYRIGHT_PACKAGE_URL`, `PYRIGHT_PACKAGE_INTEGRITY` in `scripts/pyright_profile.py`)
and unpacks one pure-JavaScript tree. A TS 7 pin is twenty URLs, twenty
integrity hashes, a platform-selection rule inside the installer, and a native
executable rather than a script run under the qualified Node major. That is a
different installation contract, not the same one.

So the pin is `typescript@5.9.3` — the last release of the JavaScript
implementation that still ships `lib/tsserver.js`.

## Finding 2 — the server: typescript-language-server 6.0.0, pinned like Pyright

Measured against the registry:

```
typescript-language-server 6.0.0
  tarball   https://registry.npmjs.org/typescript-language-server/-/typescript-language-server-6.0.0.tgz
  integrity sha512-LXtzY3UZGfghWA5eRU6/T5j1+YiGRgy14mR3GOKyTKlE1op1TYKQnLVxwBsmnXeDhGLuvzZyIHBAqvrekAITYQ==
  bytes     515,598      sha256 6e23b48efc76af4e70928cdfe62ea6e6cfef67ab4c1e7579c4e82dd284fbdfd2
  members   5            unpacked 2,424,942
  bin       lib/cli.mjs  engines  node >= 22.22.2
  dependencies: none    peerDependencies: none
```

The recomputed SRI matches the registry byte for byte. The shape is the same as
Pyright's: one tarball, one entry script, no install-time scripts, no native
code. `package/lib/cli.mjs` maps onto `PYRIGHT_SERVER_RELATIVE`
(`package/langserver.index.js`) exactly. `engines.node >= 22.22.2` is satisfied
by `QUALIFIED_NODE_MAJOR = 22` — measured `v22.23.2` on this machine — though
the profile must carry a *minor* floor, which the Pyright profile has never
needed.

Every installer bound in `scripts/install_pyright.py` is satisfied by both
tarballs: `MAX_MEMBERS = 8192` vs 5 and 132; `MAX_DECOMPRESSED_BYTES = 128 MiB`
vs 2.4 MB and 23.6 MB; `MAX_MEMBER_BYTES = 32 MiB` vs a 917 KB largest member.
No bound needs loosening.

The alternative, `vtsls`, is a wrapper around the VSCode TypeScript extension.
It is credible and reported faster on completion, but completion is not in this
product's allowlist, and it carries the VSCode extension tree rather than a
five-file bundle. `typescript-language-server` is the smaller pin for the same
read-only operations, so it is the one to take.

## Finding 3 — the server needs a `typescript` it can find, and we must hand it one

This is the "what it needs from a repository that this vault cannot assume
exists" question, and the answer is sharper than expected.

Read out of the bundle, `TypeScriptVersionProvider` resolves `tsserver.js` in
three ordered ways:

1. **UserSetting** — `initializationOptions.tsserver.path`. If it names a
   `tsserver.js` it is used directly; if it names a directory the server walks up
   to a `package.json` and takes `<dir>/lib/tsserver.js`.
2. **Workspace** — `findPathToModule(workspaceFolder, ...)`, i.e.
   `node_modules/typescript/lib/tsserver.js` in the repository.
3. **Bundled** — `require.resolve('typescript')` relative to `cli.mjs`.

Path 3 cannot work for a managed install: the tarball is five files and carries
no `node_modules`. Path 2 is exactly the thing this vault cannot assume — a
repository with `node_modules` installed. So the managed profile must take path
1 and pass its own pinned `typescript` explicitly.

Measured, that works and reports itself:

```
{"type": 3, "message": "Using Typescript version (user-setting) 5.9.3 from path
 \"/tmp/cd08/srv/typescript/lib/tsserver.js\""}
{"version": "5.9.3", "source": "user-setting"}
```

The `$/typescriptVersion` notification carries `source`, so the profile gets a
free, checkable assertion: if `source` is not `user-setting`, the session
silently picked up something we did not pin and must degrade rather than answer.
Pyright has no equivalent — its version identity is settled entirely by the
executable digest before launch. This is a genuine new obligation for the seam:
**identity that can only be confirmed after initialize**.

Consequence for the three absent things the task asks about:

- **`node_modules` absent** — irrelevant, by construction. We pass our own
  `tsserver.path`. Not degraded.
- **`typescript` package absent from the repository** — likewise irrelevant.
- **`tsconfig.json` absent** — this is the real one, and it is *degraded*, not
  refused. tsserver falls back to an inferred project with default compiler
  options. Answers still come back; cross-file resolution follows plain Node
  resolution rather than the repository's configured `paths`/`baseUrl`. The
  honest reporting is a capability limit — the same shape as Pyright's
  degradation codes — not a refusal. A refusal would be wrong: a loose `.ts`
  file with no config is a normal thing to navigate.

Measured that config *content* matters even when config is present: with
`"module": "ES2022"` against `"moduleResolution": "Bundler"` the fixture's
cross-file definition never resolved; with `"module": "ESNext"` it did. Under
`tsc --noEmit --listFiles` the corrected project loads both files and exits 0.
So "a tsconfig exists" is not the same as "the project is coherent", and the
profile cannot claim the stronger thing.

## Finding 4 — the contract-breaking difference: no readiness, silently wrong answers

This is the finding that decides the design, and it is the one I did not expect.

Pyright's session is readiness-gated throughout — `readiness()`,
`readiness_evidence()`, `_document_ready_locked()`, `_target_ready_locked()` —
and `scripts/lsp_protocol.py` hardcodes its vendor progress notifications
(`pyright/beginProgress`, `pyright/endProgress`, `pyright/reportProgress`) in
the module-level `SERVER_NOTIFICATIONS` frozenset.

`typescript-language-server` answers requests **before its project is loaded**,
and the answers are wrong without being empty or erroring. Measured on the
fixture, asking for the definition of `renewLease` at `main.ts:3`:

- before project load: one location, `main.ts:0:16` — the *import binding*,
  because the module has not been resolved yet;
- after project load: one location, `lease.ts:5:16` — the actual declaration.

References behaves the same way: 3 hits, all in `main.ts`, before load; 4 hits
including the declaration in `lease.ts` after. A caller cannot tell these apart
by inspecting the result. It is a plausible, well-formed, wrong answer.

Paired measurement, 12 fresh server processes each side, identical queries, the
only difference being whether the client waits for the readiness signal:

```
no readiness gate   correct 0/12
$/progress end gate correct 12/12   gate cost 0.674 - 0.817 s
```

The readiness signal exists but is **client-capability-gated**, which is why a
first pass concluded it did not exist at all. The server emits
`window/workDoneProgress/create` followed by `$/progress` `begin`/`end` only if
the client declares `window.workDoneProgress`, and it waits for the client's
reply to the `create` request before sending `$/progress`. With
`_CLIENT_CAPABILITIES` as currently written for Pyright, neither happens.

Event timeline, measured across four runs, times relative to `didOpen`:

```
run  progress begin  progress end  first correct answer
T0        0.121          0.982            1.031
T1        0.135          0.756            0.818
T2        0.169          0.829            0.874
T3        0.122          0.775            0.799
```

In every run the last wrong answer precedes `end` and the first correct answer
follows it. `window/workDoneProgress/create` is already in the protocol layer's
`SERVER_REQUESTS`, so the transport supports this today; what is missing is that
the client capability and the readiness *policy* are Pyright-shaped constants
rather than profile fields.

## Finding 5 — where the language boundary actually falls

Measured by counting Pyright mentions per module:

| module | lines | lines mentioning Pyright |
|---|---:|---:|
| `scripts/lsp_process.py` | 6656 | 0 |
| `scripts/lsp_positions.py` | 384 | 0 |
| `scripts/lsp_process_tree.py` | 981 | 1 (a docstring) |
| `scripts/lsp_security.py` | 2570 | 1 (a docstring) |
| `scripts/lsp_protocol.py` | 1725 | 3 (the notification allowlist) |
| `scripts/lsp_paths.py` | 29 | 5 (the managed-root constant) |
| `scripts/code_navigation.py` | 2522 | 13 |
| `scripts/pyright_session.py` | 5028 | 171 |
| `scripts/pyright_profile.py` | 1539 | 197 |
| `scripts/install_pyright.py` | 3316 | 205 |

The expensive half is already neutral. Process containment, the Job Object and
process-group work, owner/lease/failure evidence, framing, cancellation,
position encoding — 11,591 lines carrying five incidental mentions, four of them
prose. Nothing in `CODE-08` requires touching any of it.

What is genuinely Pyright-shaped is small and enumerable, and it is exactly the
list a profile should carry:

1. the pinned artifact (URL, integrity, version, relative server path, managed root);
2. the launch command — Pyright takes `--stdio --cancellationReceive=file:...`,
   `typescript-language-server` takes `--stdio` and nothing else;
3. `initializationOptions` — Pyright's are frozen constants hashed into its
   identity; TypeScript's must carry a *runtime path* (`tsserver.path`), which no
   frozen constant can express;
4. the vendor notification allowlist, hardcoded in `lsp_protocol.SERVER_NOTIFICATIONS`;
5. the client capabilities, hardcoded in `pyright_session._CLIENT_CAPABILITIES`;
6. the readiness policy — Pyright's is static, TypeScript's must gate on
   `$/progress` end;
7. post-initialize identity confirmation — Pyright has none, TypeScript needs
   `$/typescriptVersion.source == "user-setting"`;
8. the degradation-code prefix (`pyright_startup_timeout`, and so on).

Items 3, 6 and 7 are the ones that make this a seam rather than a table lookup.
A design that only parameterised the artifact and the command would compile,
install, start, and then answer wrong 0-for-12.

## What I would not do

Add TypeScript beside Pyright by copying `pyright_session.py`. The measured
boundary says the duplicated part would be the 171 Pyright-mentioning lines
inside 5028 lines of neutral LSP mechanics — 97% duplication to gain one
language, which is precisely the "double the maintenance for one language"
outcome `CODE-08` names.

## Open, and honest about it

- There is no TypeScript repository on this machine. Measured:
  `find / -name tsconfig.json -not -path '*/node_modules/*'` returns nothing, and
  no `node_modules/typescript` exists anywhere. Any TypeScript evidence in this
  work is from a fixture I wrote, and is labelled as such.
- The 0.67-0.82 s readiness cost was measured on a two-file fixture. A large
  repository's project load will be longer, and the `warm_overhead_p95_ms = 30`
  gate is a *warm* gate — it does not cover cold project load. Whether the
  TypeScript path needs its own cold-start budget is not settled here.
- `tsgo --lsp` (TypeScript 7 native) is the upstream direction and should be
  re-examined once it is pinnable as a single artifact per platform with
  published integrity, because it removes the `tsserver.path` problem entirely.

## Sources

- [typescript-language-server on npm](https://www.npmjs.com/package/typescript-language-server)
- [typescript-language-server on GitHub](https://github.com/typescript-language-server/typescript-language-server)
- [Announcing TypeScript 7.0](https://devblogs.microsoft.com/typescript/announcing-typescript-7-0/)
- [vtsls](https://github.com/yioneko/vtsls)
- [LSP 3.18 specification](https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/)
- Registry metadata for `typescript-language-server@6.0.0`, `typescript@5.9.3`,
  `typescript@7.0.2`, `@typescript/typescript-linux-x64@7.0.2`, fetched
  2026-08-28.
- Local measurement: `/tmp/cd08/{probe,settle,gate}.py` against the fixture
  described in Finding 4.
