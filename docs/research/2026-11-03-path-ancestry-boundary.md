# Shared path ancestry policy

2026-11-03, refinement under the approved decomposition completion mandate.
The initial complete sqlite_io packet measures 100325 tokens with its own tests:
325 over the user's bound. Its only bounded_io call is _acceptable_ancestor,
which brings all unrelated bounded reading/decoding logic into the dependency
closure. Separate the existing three cohesive predicates (_root_controlled,
_system_symlink, _acceptable_ancestor) into llm_wiki.path_ancestry. Both callers
import that owner directly; bounded_io retains identical legacy aliases. Preserve
function ASTs and comments. This removes a real source dependency, not context.

Research (three independent primary publishers, checked today):
- Linux openat2 documents kernel resolution restrictions and their OS-specific
  nature: https://man7.org/linux/man-pages/man2/openat2.2.html
- Go's traversal-resistant API explains the race between pathname validation and
  opening, and the platform-dependent guarantees of rooted operations:
  https://go.dev/blog/osroot
- Python 3.10 pathlib distinguishes lstat, stat, directory and symlink queries:
  https://docs.python.org/3.10/library/pathlib.html

Select a behavior-preserving Python policy owner. A Linux-only openat2 wrapper,
Go runtime dependency, blanket symlink prohibition or a new filesystem wrapper
would change compatibility or semantics. No such change is authorized here.
The policy rejects reparse points, accepts real directories, and permits only
existing administrator-owned symlink cases. It is a predicate, not an atomic
filesystem sandbox: callers retain their identity and no-follow protections.
Python 3.10 remains the compatibility floor despite upstream EOL.

The root-owned parent test and link-owner test remain distinct. Windows refusal,
metadata errors and untrusted link rejection retain their exact outcomes.
Retarget tests that patch _system_symlink to the true owner, preserve assertions,
and verify bounded IO plus probe regressions. This small owner gets its own full
packet, import guard, real-path and simulated metadata tests. sqlite_io's packet
retains all its actual dependencies and tests, including this owner's source.

Evidence: fresh built-in generation generation-18e465c78ce42d35-5cfa63fb
covered the staged SQLite extraction; the caller graph found both ancestry
consumers and was supplemented by a source-wide search. Baseline reader/probe
suite: 64 passed, one Windows skip. After extraction the same reader/probe tests
plus SQLite/fingerprint owners passed 79 cases with one skip. New isolated
policy and SQLite tests cover ownership, real read-only refusal, close-on-error,
streaming across the batch boundary, stop propagation and replaced-directory
cleanup. The bounded reader's import guard now allows only the exact shared
owner, with negative checks for other modules in the same package; it does not
allow the entire llm_wiki package. Original behavioral assertions remain intact.
