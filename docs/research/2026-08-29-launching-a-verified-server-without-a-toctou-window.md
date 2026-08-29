# Launching a verified server without reopening a TOCTOU window

*2026-08-29. Written because `CODE-08` could not produce a TypeScript answer
without changing how a verified language server is executed, and the existing
strategy is a security property, not an implementation detail.*

One-sentence summary: the descriptor launch that closes the verify-then-execute
race on Pyright is **measurably unusable** for a package-shaped ESM server, so
this build keeps it for servers it still fits and adds a second strategy that
executes a digest-verified copy plus a manifest this build authors, reading
nothing from the operator-writable install root at exec time — which preserves
the window that mattered and reopens one that only a same-uid process can enter.

---

## What the existing strategy actually guarantees

`_LaunchServerGuard._posix_launch` (`scripts/pyright_session.py`) does this:

1. `stat` the installed server, open it with `O_NOFOLLOW|O_CLOEXEC`, and confirm
   the opened inode is the one just measured.
2. Copy it through that descriptor into a private snapshot under the owner's
   scratch root, hashing as it copies, and refuse unless the digest equals the
   pin.
3. Open the snapshot, `unlink` it, and hand **the descriptor** to `node`.

Step 3 is the part that matters. After the unlink there is no path left to
attack: the bytes `node` executes are reachable only through an inherited file
descriptor on an inode with no directory entry. Nothing — not another user, not
the operator's editor, not a process of the same uid — can substitute them.
That is a complete closure of the verify-then-execute race, and it is the
strongest form available to an unprivileged process on POSIX. It is the same
idea the kernel offers as `memfd_create(2)` plus `F_SEAL_WRITE`, whose stated
purpose is to "address time-of-check-to-time-of-use race conditions"
([memfd_create(2)](https://www.man7.org/linux/man-pages/man2/memfd_create.2.html)).

Two limits of that guarantee are worth stating before anything is traded away,
because both are already true at HEAD:

* **It covers one file.** Pyright's `langserver.index.js` is a self-contained
  webpack bundle, so one file is the whole server. It was never a general
  property of "the managed server"; it was a property of that artifact's shape.
* **It does not cover the runtime.** The TypeScript profile passes
  `tsserver.path` at a 9 MB `typescript/lib/tsserver.js` that the launched
  server reads from the install root at run time. `install-manifest.json`
  records its digest at install time (`runtime_sha256`), and nothing re-checks
  it at launch. Measured by reading the manifest on this machine.

## The measurement that removes the choice

The TypeScript server's entry is `package/lib/cli.mjs`, an ES module. Two
independent failures, both reproduced here on node v22.23.2:

```
Module._compile(<cli.mjs>)                       -> ERR_REQUIRE_ASYNC_MODULE
node /proc/self/fd/3 --stdio                     -> ENOENT open '/package.json'
node --preserve-symlinks-main /proc/self/fd/3    -> ENOENT open '/proc/self/package.json'
```

The first is the shallow one: `_NODE_MAIN_LOADER` compiles source as CommonJS
and an ESM entry cannot be compiled that way.

The second is the one that decides the design, and it survives fixing the first.
`cli.mjs` line 26365 reads

```js
const {version: version} = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), ...));
```

`import.meta.url` "is defined exactly the same as it is in browsers providing
the URL of the current module file", which enables "relative file loading"
([Node.js ESM documentation](https://nodejs.org/api/esm.html)). It is not
settable, not overridable by a flag, and not derivable from a descriptor. When
the module's URL is `file:///proc/self/fd/3`, `../package.json` resolves outside
any directory this process controls. I tested `--preserve-symlinks-main`
specifically to check whether keeping the descriptor path unresolved helped; it
moves the miss from `/package.json` to `/proc/self/package.json` and helps
nothing.

**So the descriptor launch is not being traded away for convenience. It cannot
execute this artifact at all.** Any strategy that runs this server must give the
module a URL inside a directory that contains a `package.json`.

## What was considered

**Launch in place from the install root.** Simplest, and gives up the most: the
install root is `cache/code-tools/...` inside the vault, which the operator's own
tools write — the installer, an editor, `git checkout`, the nightly self-update.
That is exactly the location the copy-aside exists to get out of the exec path.
Rejected.

One correction to an earlier draft of this note, because the measurement went the
other way. I wrote that the tree is group-writable and implied another user could
reach it. Measured on this machine:

```
drwx------  cache/code-tools
drwxrwxr-x  cache/code-tools/typescript-language-server
drwx------  cache/code-tools/typescript-language-server/6.0.0
```

The middle directory is indeed `0775`, but its parent is `0700`, so no other user
can traverse to it and the loose mode buys an attacker nothing. The population
that can write the install root is this uid — which is the operator and
everything the operator runs. That is a smaller claim than the draft made, and it
is still the claim the design rests on. (The inconsistent `0775` is worth
tightening in the installer for its own sake; it is not a finding this work
closes, and it is not load-bearing here.)

**Verify an immutable directory instead of one file.** The natural reading of
"verify the artifact" for a package. It needs per-file digests, and
`install-manifest.json` records exactly two (`server_sha256`, `runtime_sha256`),
so it would mean a manifest schema change and re-verifying every file in a
2.4 MB package on every session start. It also would not buy the property: the
directory would still be a directory some process can write into.

**An operator-unwritable launch directory.** Create the launch tree mode `0500`
inside the owner's `0700` scratch root. Real against another user and against
the operator's own tools, which is the threat the copy-aside addresses. Not
sealable against the same uid: `chmod` is available to whoever owns the inode,
and no unprivileged POSIX call makes a directory immutable
(`chattr +i` needs `CAP_LINUX_IMMUTABLE`). Kept, as the floor, not as a closure.

**`memfd_create` + `F_SEAL_WRITE`, exposed at `/proc/self/fd/N`.** This is the
textbook answer and it is genuinely airtight for the bytes — a sealed memfd
cannot be modified, and a second process can open it through `/proc/<pid>/fd/<fd>`.
It fails for exactly the reason above: a memfd has a path but no *directory*, so
`../package.json` still escapes. Linux-only besides.

**Synthesize the package root.** Measured, and it is the one that wins.

## What was measured, and what was taken

I drove the installed `package/lib/cli.mjs` through an `initialize` handshake
from two launch directories: one holding the shipped `package.json`, one holding
a three-field manifest I wrote. Identical results — 1875 bytes of LSP traffic,
same `window/logMessage`, and the identity notification the profile gates on:
`Using Typescript version (user-setting) 5.9.3`.

Reading the bundle confirms why: the *only* consumer of `../package.json` is
`{version}`, handed to commander's `.version(version)`. Nothing else in the file
reads it.

**Taken: the launch set is the digest-verified entry plus a manifest this build
authors.** The guard copies the entry through the held descriptor into the
owner's private scratch root, verifies the digest exactly as it does today, then
places it at `<owner>/launch-<nonce>/lib/cli.mjs`, writes
`<owner>/launch-<nonce>/package.json` from a constant on the profile, seals the
tree read-only, re-reads the sealed path to confirm the bytes at the path node
will open are the bytes that were hashed, and launches that path. The tree is
removed when the guard closes.

This is deliberately *not* "copy the package root". Copying the shipped
`package.json` would carry unverified bytes out of the operator-writable install
root and into the exec path — the precise thing the strategy exists to prevent.
Authoring the manifest means **nothing unverified from the install root is read
at exec time**, which is a stronger statement than the copy-the-directory option
could make, and it is why the residual window below is as small as it is.

Profiles declare which strategy they get. Pyright declares none and keeps the
descriptor launch byte for byte; nothing about the Python path changes.

## The window that remains

Stated plainly, because trading it silently is the failure this note exists to
prevent.

| Who | Before (Pyright, descriptor) | After (package launch) |
|---|---|---|
| Another local user | closed — and already excluded by the `0700` ancestor | closed — target is `0700` owned scratch, `0500` tree |
| Operator's tools writing `cache/`: installer, editor, `git checkout`, nightly self-update | closed | closed — the install root is not read at exec time |
| A process racing as **this same uid** | closed | **open** |

The third row is the honest loss. Between the guard's post-seal verification and
`node`'s `open()` of the entry, a process of the same uid can `chmod` the launch
directory and substitute the file. Nothing unprivileged prevents that, because
ESM entry resolution is path-based and offers no descriptor entry point. Two
mitigating facts, offered as facts and not as excuses: the window is the
microseconds between the read-back and `exec`, under a directory named with a
fresh 128-bit nonce; and a same-uid attacker can already `ptrace` the very
process doing the verifying, so this row was never a boundary this design could
hold on its own. Row two is the one that mattered in practice — an operator
editing or reinstalling under `cache/` while a session starts — and it is
unchanged.

**Platforms.** POSIX only. `__enter__` takes the descriptor branch under
`os.name == "posix"`; Windows already verifies and launches by path, so the
Windows path is unchanged by this work and carries the same-uid window it always
carried. On POSIX, Pyright is unchanged and keeps the complete closure;
TypeScript gets the table above.

**Not claimed.** That the launched server's *runtime* (`tsserver.js`) is
verified at launch — it is not, it is verified at install and read from the
install root, and that is a pre-existing gap this work does not close. That the
same-uid window is closed. That the strategy generalizes to a server needing
more of its package than a version string; the profile field is the place a
future server would say so, and a server that reads more would need the
directory-verification option rejected above.

## Sources

- [memfd_create(2) — Linux manual page](https://www.man7.org/linux/man-pages/man2/memfd_create.2.html) — sealing as the kernel's own TOCTOU answer, and why it needs no filesystem path.
- [Modules: ECMAScript modules — Node.js](https://nodejs.org/api/esm.html) — `import.meta.url` semantics and relative loading.
- [What Is Package Integrity? — Endor Labs](https://www.endorlabs.com/learn/what-is-package-integrity-definition-and-best-practices) — pinned digests verified at use, not only at fetch.
- [How to Protect Against Software Supply Chain Attacks: a 2026 playbook](https://shaharia.com/blog/protect-software-supply-chain-attacks-2026/) — SLSA level 3 as the 2026 floor for production artifacts; verification at admission rather than continuously.
- Local measurement, 2026-08-29, node v22.23.2, `typescript-language-server@6.0.0`, `typescript@5.9.3`, digests as recorded in `cache/code-tools/typescript-language-server/6.0.0/install-manifest.json`.

## Measured outcome (2026-08-29)

Paired, on the same fixture checkout, through `mcp_server._execute_tool_call`.
"Before" is a clean `git archive HEAD` extract, so it is the shipped code and not
a reverted working tree.

```
BEFORE  definition  status=error  readiness=not_ready   total=0
                    warnings=['source document validation failed', 'typescript_startup_failed']
BEFORE  references  status=error  readiness=not_ready   total=0   (same warnings)

AFTER   definition  status=ok       readiness=query_ready  total=1
                    src/lease.ts:6:16          (the declaration)
AFTER   references  status=partial  readiness=query_ready  total=4
                    src/lease.ts:6:16, src/main.ts:1:16, src/main.ts:4:13, src/main.ts:5:14
```

Blocker 1, at the guard, before: for a `.mjs` entry the launch command was
`/proc/self/fd/5`, so `../package.json` resolved to `/proc/self/package.json`.
Blocker 2, before: `compute_workspace_revision` on the TypeScript checkout
returned `entries == []`; after, `['src/lease.ts', 'src/main.ts', 'tsconfig.json']`.

Two smaller things the end-to-end run exposed, both fixed here:

* `provenance[].provider` still carried the literal `"pyright"` on a TypeScript
  answer. `provider.name` had been corrected at the MCP seam; the provenance row
  had not, so the field whose job is to say where an answer came from was saying
  something false.
* The reported `readiness` was stale. It is settled while the document is opened,
  which for a progress-gated server is necessarily before the project load ends;
  the query itself does wait (`_begin_document_query` calls
  `_await_progress_gate`), so the gate was in force, but the first query of a
  session reported `protocol_initialized` for an answer that had waited. Now
  recomputed after the wait, for progress-gated profiles only, so Pyright takes
  no extra round trip.

Not claimed: that `readiness` was ever a bypass. It was a reporting defect, and
the answers were identical and correct either way — verified over three rounds.
