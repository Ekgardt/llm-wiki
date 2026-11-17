# The installed Codex provider choice travels atomically

Research and qualification date: 2026-10-07. This implements the explicitly
approved installed-provider transport described in `docs/STRUCTURE.md`.

An already running host does not acquire environment changes made later to a
shell profile or scheduler. The old hook and MCP commands therefore left model
selection to that old host. Two retained regression cases reproduce the missing
installed choice without calling a model.

The existing seven `PROVIDER_ENV_KEYS` are the complete transport allowlist. An
owned command carries their non-secret installed bundle as canonical base64 JSON.
The same Python process applies that entire bundle only when none of those keys
exists in its environment. An explicitly present empty value is an override too.
One explicit key preserves the whole host environment; the bootstrap never fills
the remaining keys from another provider. Test-only `fake` and unrelated API-key
variables are not installed. No top-level Codex environment field, runtime file,
new process, provider setting, or global Codex preference is introduced.

Hooks, the two installers' MCP block, and doctor use the same renderer. Doctor
obtains the expected bundle from the existing validated install manifest,
transaction, and exact desired snapshot. A snapshot by itself, current process
environment, or edited live hook cannot establish expected installation state.
The reader checks vault/state/home binding, a single matching managed resource,
the established install-control state, desired bytes, and fresh before/after
record equality. Missing or unverifiable provenance is reported as unverified.
Codex's own native hook trust remains a separate check: changed commands still
require the normal `/hooks` review. MCP registration remains unowned by uninstall.
Existing foreign-entry, disabled-entry, preimage, rollback, and CAS checks remain.

## Script execution alternatives

Python 3.10.20 demonstrated a real `runpy.run_path` mismatch: a relative script
argument has an absolute `__file__` and relative `sys.argv[0]` under direct Python,
but neither relative nor absolute `run_path` input preserves both. The original
parity failure is retained. Private runpy patches and weaker assertions were
rejected.

The approved same-process engine instead reads source bytes, compiles them with
the absolute filename, and executes them in a fresh `__main__` module. It retains
the original literal argument vector and replaces the `-c` import-path entry with
the direct script directory. An additional retained regression showed that merely
inserting the script directory left an extra current-directory import entry.
Existing isolated and safe-path interpreter policies remain intact.
The module has the direct-script loader, package, specification, cache, and
builtins metadata. Relative and absolute invocation, local imports, UTF-8 binary
stdin, binary stdout/stderr, `SystemExit`, and same-process ownership are covered
by actual Python 3.10 tests. No target bytecode cache is reused. Uncaught exceptions
are not suppressed; bootstrap frames naturally remain in their traceback, so this
is not a claim of byte-identical uncaught traceback output.

## Platform boundaries and remaining qualification

The exact Codex 0.160.0 hook runner uses the configured shell; its Windows default
is `COMSPEC`/`cmd.exe` with `/C` and a raw quoted command line. Microsoft's C argv
quoting rules and Python's `list2cmdline` alone do not prove shell safety. The
renderer explicitly quotes every argument and handles trailing backslashes;
on Windows, ownership parsing uses the already required pywin32 native argv
binding. Desired platform command pairs are checked against the same full POSIX
argument projection, so a Linux manifest check does not pretend to run a Windows
parser. The former unquoted inline-program metacharacters are distinct from the
pre-existing general CMD percent-expansion risk. CMD percent expansion and a
configured PowerShell's interpretation still require real Windows controls.
Linux subprocess parity does not qualify those paths. Actual
Windows command execution and native hook trust are still outstanding; this
research note does not claim the candidate is ready for Windows installation.

The direct legacy command recognizer remains only for ownership-preserving
upgrade and exact recorded rollback. It cannot prove an installed provider bundle.
Remove it when retained manifests and supported migrations no longer name those
commands. No complete legacy-cleanup claim is made while that contract is needed.

## Primary sources and versions

- [Official Codex hooks documentation](https://learn.chatgpt.com/docs/hooks):
  native commands and trust review; fetched on the qualification date.
- [Python 3.10 runpy](https://docs.python.org/3.10/library/runpy.html),
  [compile and exec](https://docs.python.org/3.10/library/functions.html#compile),
  and [main-module behavior](https://docs.python.org/3.10/library/__main__.html):
  public execution semantics. Runtime controls use Python 3.10.20; the current
  maintained 3.10 documentation may describe a later patch release.
- [Python command-line and import-path policy](https://docs.python.org/3/using/cmdline.html):
  direct-script versus `-c` import entries, isolated mode, and the Python 3.11+
  safe-path policy. Current documentation identifies the version that introduced it.
- [Microsoft C command-line parsing](https://learn.microsoft.com/en-us/cpp/c-language/parsing-c-command-line-arguments?view=msvc-170):
  argv quoting rules, distinguished here from shell interpretation.
- [Official Codex 0.160.0 command runner](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/hooks/src/engine/command_runner.rs):
  the actual Windows shell dispatch implementation.

Installed tooling observed during research: Codex CLI 0.160.0, uv 0.12.3,
Python 3.10.20 for compatibility tests, and pywin32 312 in the existing Windows
dependency lock. No dependency was added. Model calls and token savings are not
part of this fixture qualification.

## Stable installation evidence, 2026-10-07

The initial implementation compared bytes before and after the proof. Four
retained causal controls showed that replacing the manifest, transaction or
selected desired preimage with another file containing the same bytes, or
changing POSIX permissions, could still supply an expected bundle. Identical
bytes alone do not prove a stable installation evidence chain.

The successor uses an invocation-local read scope. Every actual record and
preimage read still reads fresh bytes. Its descriptor stays open while schema,
transaction, resource and desired-projection checks run. Before returning the
bundle, each used file is reopened and its descriptor identity, complete bytes,
permissions and resolved path are compared; the original descriptor is checked
again. The scope keeps neither a healthy verdict nor reusable authority. Nested
scopes restore their predecessor and every exit closes the descriptors. Other
installation operations retain their existing reader contract. Directory
modification timestamps are not file identity and are intentionally excluded.

Python's [descriptor metadata API](https://docs.python.org/3.10/library/os.html#os.fstat)
permits comparison of metadata from the actual open files. Windows path and
handle change timestamps can have different semantics, so this proof compares
handle metadata with handle metadata; [Microsoft's FILE_BASIC_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_basic_info)
distinguishes creation and change times. On Windows the proof also compares the
actual handle's owner, group and DACL security descriptor through the already
installed pywin32 binding. [MITRE CWE-367](https://cwe.mitre.org/data/definitions/367.html)
explains why a path check before use is insufficient and why evidence must be
rechecked after use. These three independent primary publishers were checked
on 2026-10-07. This is a cooperating-reader consistency proof, not an OS sandbox
or a claim of atomic multi-file reads against hostile concurrent writers.

The Linux permission and same-byte replacement controls do not qualify Windows
ACL changes or configured Windows shell execution. Actual Windows controls and
native trust remain required before changing owned installed configuration.

The successor also supplies native Windows controls for a same-byte proof-file
DACL mutation and configured `pwsh` / `powershell.exe` execution. The latter
uses Codex 0.160's configured-shell whole-command argument route; it compares
script metadata, local imports, binary stdin, installed provider choice and
stdout/stderr/exit behavior with the original direct-script route. The shell's
exit status is compared with that original route, rather than assuming it is
the Python child's numeric exit status. These controls are genuinely skipped
on Linux. `uv` remains the existing bare executable token; the bootstrap's
arguments are quoted. This does not establish safety for arbitrary Windows
CMD percent expansion or PowerShell dollar interpolation in vault paths.
A GitHub Actions outer PowerShell step alone is not configured-hook parity.

A second retained causal check found six refusals missing from the original
codec and emitted program: canonical provider selectors containing whitespace
around case variants of `fake` were accepted, although the existing runtime
selector strips whitespace and lowercases before choosing `fake`. Both install
codec and emitted program now use that same normalization only for the forbidden
test-provider comparison. Every non-test value remains byte-for-byte unchanged;
there is no new provider allowlist. Explicit host key presence, including an
empty value, continues to preserve the entire original host environment.
