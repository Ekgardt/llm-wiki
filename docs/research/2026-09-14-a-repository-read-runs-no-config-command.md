# A repository read runs no command from the repository's config

Dated 2026-09-14. Item 3.2 of `docs/AUDIT-2026-09-14-2.md`. The research before the fix.

## What was found

- `core.fsmonitor` may name a program, and Git runs it whenever it refreshes the index.
  A repository's own `.git/config` can set it. Reproduced on this machine (git 2.43.0)
  in a scratch repository whose `core.fsmonitor` is a script that touches a file:
  `git status`, `git ls-files` and `git worktree remove` ran the script;
  `git rev-parse`, `git log -1 --format=%H`, `git worktree list`, `git config` and
  `git merge-base` did not. With `-c core.fsmonitor=false` on the command line,
  `git status` did not run it.
- The codebase already does this in two places: `workspace_revision._git_command` and
  `impact_analysis` (`-c core.fsmonitor=false`). `sanitized_git_environment` strips
  ambient Git variables but cannot carry config (and a test pins that it carries no
  `GIT_CONFIG_*`).
- Places that run an index-refreshing command without it:
  - `repository_index._git_text` — `ls-files -z` (two callers), on any repository the
    operator or an agent indexes; reachable through MCP `get_architecture` and the
    nightly `refresh-all`;
  - (`repository_scope._git_output`, the shared identity probe, runs only `rev-parse`
    and `for-each-ref`, which did not run the script; it is left as it is);
  - `repository_worktrees._git`;
  - `cleanup_worktrees` — `git status --porcelain` and `git worktree remove` in every
    worktree it inspects;
  - `lint_memory._git_tracked_paths` — `ls-files` in the vault checkout.
- The code graph: `_git_text` ← `repository_index` (`_submodule_refusal`, `ls-files`
  readers); `sanitized_git_environment` ← `_git_text`, `repository_worktrees._git`,
  `repository_scope._git_probe_process`, `workspace_revision`, `impact_analysis`.

## Practice on this date

- Git's own documentation: `core.fsmonitor` set to a path runs that hook program, and
  configuration given with `-c` overrides the repository's
  ([git-config core.fsmonitor](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corefsmonitor),
  [git -c](https://git-scm.com/docs/git#Documentation/git.txt--cltnamegtltvaluegt)).
- The class is known: shell prompts and editors that ran `git status` in an untrusted
  directory executed `core.fsmonitor` (CVE-2022-24765 and the prompt advisories that
  followed), and the standard mitigation is to disable the option on the command line.

## The decision

- `repository_scope.GIT_NO_CONFIG_COMMANDS = ("-c", "core.fsmonitor=false")`, used by
  `repository_index._git_text` and `repository_worktrees._git`. `cleanup_worktrees` (standalone, imports nothing from
  the scripts) and `lint_memory` carry the same two arguments.
- Only the option reproduced as executing on these commands is disabled; nothing is
  claimed about other config keys.

Files: `scripts/repository_scope.py`, `scripts/repository_index.py`,
`scripts/repository_worktrees.py`, `scripts/cleanup_worktrees.py`,
`scripts/lint_memory.py`, `tests/test_a_repository_read_runs_no_config_command.py`,
`docs/research/2026-09-14-a-repository-read-runs-no-config-command.md`.
