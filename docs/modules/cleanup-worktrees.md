# Worktree cleanup CLI

The existing `scripts/cleanup_worktrees.py` is qualified in place. Its production
bytes, two original test files, public flags and environment-selected branches
are unchanged. The full packet includes the shared repository_scope implementation
and guide. No parallel deletion engine or new runtime state is introduced.

Default invocation is a dry run. `--apply` performs eligible removals;
`--prune-stale-metadata` is separately named and stays a dry run without apply.
Interactive force removal still requires apply, interactive mode and a positive
per-item answer. Main, locked, missing/prunable and out-of-scope worktrees retain
their protections. The root scope is resolved descendants of the existing three
agent roots. Normal removal retains Git's clean-tree and branch deletion checks.
The shared config-command override and existing bounded Git wait stay in place.

This is not an atomic transaction across worktree and branch removal. Existing
code removes the worktree before `git branch -d`; that second step may refuse
when the topic is contained by the integration branch but not by HEAD/upstream.
The failure propagates and the branch remains; there is no automatic rollback of
the removed directory and no force fallback. A real temporary-repository test
records this behavior explicitly. Qualification does not claim to repair or hide
that partial-operation limitation, or authorize applying this CLI to live data.

## Research — 2026-11-04

Checked [Git worktree](https://git-scm.com/docs/git-worktree) and
[Git branch](https://git-scm.com/docs/git-branch),
[Python subprocess](https://docs.python.org/3.14/library/subprocess.html) and
[OWASP command injection defense](https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html).
Native Git owns removal refusal; separate argv and option terminators preserve
argument boundaries, and shared policy prevents the reproduced configuration
command execution. Keep one coherent CLI rather than replacing Git operations,
adding another subprocess layer or forcing a refused branch deletion. No Git or
Python upgrade, timeout change or new restriction is introduced.

Complete historical config-command and deadline reports accompany this packet.
The older description of a local policy copy is historical: the later deadline
report and current source use repository_scope as the shared authority. Original
real-Git and parser/scope tests remain intact. Cross-repository config-command
integration and the all-script child-deadline guard remain mandatory checks
under their own module owners.

Gates: unchanged source/test hashes, isolated import allowing repository_scope,
real Git refusal with retained refs, actual complexity/shape, measured test weights,
full dependency guides/authority files and exact packet reconstruction within
100000 tokens. No whole-project completion, live installation or LLM cost saving
is claimed by this qualification.
