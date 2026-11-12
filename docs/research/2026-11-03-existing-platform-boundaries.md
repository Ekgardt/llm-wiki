# Existing platform boundaries

2026-11-03; completion mandate; candidate baseline e91402e0.

Qualify existing independent owners in place: windows_workspace owns Windows
handle-relative filesystem operations; lsp_process_tree owns spawned-process
containment and cleanup. No production relocation, new wrapper, dependency,
API, timeout, native flag or cleanup rule is proposed. Their public import paths
remain unchanged. Moving these cohesive leaves merely to change paths would add
compatibility work without reducing coupling.

Packets must include the full existing focused test files. The process packet
also needs tests/fake_lsp_server.py: subprocess fixtures are real dependencies.
Both include common pytest bootstrap and the shared import-isolation checker.
A packet that exceeds 100000 tokens remains unqualified; tests or fixture modes
must not be omitted to make a number pass. Other integration suites retain their
own future owners in the complete-project registry.

Research checked today against independent primary publishers:
- Python 3.10 ctypes and subprocess documentation: explicit native signatures,
  subprocess lifetime and platform differences. The project still requires 3.10;
  upstream marks it unsupported. No version upgrade is part of qualification.
  https://docs.python.org/3.10/library/ctypes.html
  https://docs.python.org/3.10/library/subprocess.html
- Microsoft CreateFileW, maximum path length and Job Objects: sharing and reparse
  flags, UTF-16 path constraints and job assignment are material contracts.
  https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
  https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation
  https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
- Fowler Bounded Context: keep distinct responsibilities and explicit relations.
  https://martinfowler.com/bliki/BoundedContext.html
Alternative: combine both owners in a generic platform service. Rejected because
filesystem handles and process lifetime have different contracts and consumers.
A platform abstraction rewrite would change qualification scope without need.

The POSIX process group is not a hostile-process sandbox; setsid escape remains
unsupported. Windows-specific tests skipped on Linux are not native proof.
Initial unchanged focused tests: 19 passed, 39 Windows skips. Source import
inspection finds only stdlib dependencies; fresh indexed graph and direct caller
inspection must precede implementation of the qualification guards.

Fresh source generation: generation-18e4631272be9c56-deeece21, e91402e0.
MCP graph workers reported abandoned work; a separate process called the same
built-in code_graph reader against that generation. Confirmed filesystem callers
include doctor and LSP ownership. Class construction edges are incomplete; direct
source search additionally finds profile/session, security, installer, reliable
IO and model-client consumers. Evidence: logs/platform-leaves-graph.json and
logs/platform-leaves-consumers.txt. No consumer imports or callbacks change.

LSP qualification is deferred: including the real fixture, the unread-/proc-entry
regression and boundary helper already costs 100705 tokens before its own guide,
manifest and guards. It is not counted as a ready module. The filesystem owner
can be qualified independently without changing native code.

Filesystem qualification: 357 related checks passed, 38 platform skips; focused
Python 3.10 checks passed 3, skipped 10 Windows cases. Three weighted module tests
passed; local CCN/shape and Ruff passed. Production source and original focused
tests remain byte-identical. All 33 artifacts round-trip exactly and stay within
the token ceiling. The full e91402e0 run is still in progress and predates only
these new documentation/import-boundary checks; no new full pass is claimed.
