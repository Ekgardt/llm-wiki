# A bootstrap child failure keeps its cause

Research date: 2026-09-29. Focused diagnostic correction qualified.

Follow-up: the missing retry described below was subsequently implemented and
qualified, including conditional creation and actual commit-state verification.
See `2026-09-29-an-incomplete-project-bootstrap-is-revisited.md`.

`session_start_project_state._bootstrap_new_project` discarded both the child
return code and caught exceptions. A real synthetic child that exits 7 with a
diagnostic and an injected timeout both leave no error log on the original code.
The success control exits zero without a failure record. The problem is the
parent's discarded outcome, independent of why any particular child failed.

Set `check=True` on the existing subprocess call, then record the caught exception
type, message and captured stderr through the existing, now-redacting error sink.
Keep host session availability and the existing measured timeout. There is no new
process, directory, schema, environment contract or retry limit. This changes
diagnostic handling, not bootstrap publication semantics.

Primary sources checked today:

- [Python subprocess](https://docs.python.org/3/library/subprocess.html#subprocess.run):
  `check=True` raises CalledProcessError for nonzero completion, retaining captured
  stderr; TimeoutExpired also retains stderr, possibly as bytes.
- [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html):
  operational failures need useful diagnostics with credentials removed.
- [MITRE CWE-117](https://cwe.mitre.org/data/definitions/117.html):
  untrusted error text must not inject log records.

Alternatives: manually inspect returncode (duplicates the subprocess check), drop
stderr (loses the child's explanation), propagate every error to the host (changes
session behavior), or keep swallowing errors (leaves the original defect).
The standard checked call plus the existing protected sink is the smallest
compatible correction. Python 3.12.3 is installed; the used APIs also exist in the
supported Python 3.10 baseline. No dependency or technology replacement is needed.

This does not fix the separate missing retry: after state.md exists, ordinary
session startup bypasses this helper. The previous source comment's claim that
the next session necessarily retries was false and is removed. Qualification of
retry scheduling and missing bootstrap context remains outstanding; no successful
installation or full project completion is claimed.

The original code failed two regressions while four controls passed. Corrected
code passed 28 checks in 37.50 seconds, including the actual subprocess failure,
injected timeout, successful child, hook log protections, project slug checks,
Lizard/AST complexity gate and broad-handler diagnostic guard. Ruff passed.
The full Bandit scan now has 351 warnings and no parse errors; B110 decreased
from 19 to 18. No warning was suppressed. Proof prefix:
`logs/audit-2026-09-29-completed-repair-bootstrap-diagnostics-`.
