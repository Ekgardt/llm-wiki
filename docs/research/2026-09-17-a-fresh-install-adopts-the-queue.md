# A fresh install adopts the queue

Dated 2026-09-17. Findings I-A1 and I-A2 of the third audit (high, reproduced). The research
before the fix.

## What was found

- `install.sh` step 8b reads the adoption state with
  `STATE="$(check --json | python3 -c … || echo unknown)"` under `set -o pipefail`.
- The check exits 1 for a `fresh` and for an `upgrade-required` vault, because both are
  reported as degraded. The parser still prints `fresh`, then the failed pipeline runs
  `|| echo unknown`, so the variable holds two lines — `fresh` and `unknown` — and the
  `case` falls through to the branch that only prints a warning. Reproduced by the audit
  against an empty temporary vault: `adoption_state: fresh`, exit 1, fall-through.
- So every fresh macOS or Linux install ends with session capture refused
  (`legacy_protocol_unquiesced`, issue #17 again). No test runs this step.
- `install.ps1` has no adoption step at all, so a fresh Windows install ends the same way,
  while `README.md` says "the installer runs it".

## Practice on this date

- "If `pipefail` is enabled, the pipeline's return status is the value of the last
  (rightmost) command to exit with a non-zero status, or zero if all commands exit
  successfully" ([Bash manual, Pipelines](https://www.gnu.org/software/bash/manual/html_node/Pipelines.html)).
  A fallback written as `a | b || echo x` therefore fires when `a` fails even though `b`
  already printed its answer. The remedy is to separate the two facts: capture what the
  first command printed, then parse it, and let only a parse failure choose the fallback.
- A step that cannot be run in CI is tested as a function with a stand-in for the tool it
  calls — the shape `tests/test_installer_bootstrap.py` already uses for the bootstrap.

## The decision

- The state is read by one shell function, `adoption_state_of`, which captures the check's
  output whatever its exit status and parses it separately; only unparsable output is
  `unknown`. A test runs that function with a stand-in check that prints `fresh` and
  exits 1.
- `install.ps1` gains the same step with the same three outcomes: adopted, adopted now,
  or named with the command to run.

Files: `install.sh`, `install.ps1`, `tests/test_a_fresh_install_adopts_the_queue.py`,
`docs/research/2026-09-17-a-fresh-install-adopts-the-queue.md`.
