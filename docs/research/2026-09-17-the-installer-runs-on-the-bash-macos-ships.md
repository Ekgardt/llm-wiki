# The installer runs on the bash macOS ships

Dated 2026-09-17. A correction to the fix of finding I-A5, made the same day after CI.

## What was found

- The first fix of I-A5 made `install.sh` refuse any bash older than 4.4 and tell the user to
  `brew install bash`. The macOS installer job in CI runs the bootstrap tests under the
  system `/bin/bash` 3.2.57: ten tests that passed before now fail on that refusal. The part
  of the installer they cover had always worked on 3.2.
- macOS is a supported platform (user LaunchAgent). Asking every macOS user for a second
  shell is a regression in itself when the installer holds exactly two constructs 3.2 lacks:
  one `mapfile` (the uv sync arguments) and one possibly empty array expanded under `set -u`
  (`IDE_HOOK_ARGS`). The two `AGENT_STATUSES` expansions are already guarded by a count.

## Practice on this date

- bash `NEWS`, new features of bash-4.0: "There is a new `mapfile' builtin to populate an
  array with lines from a given file." A `while IFS= read -r` loop does the same on every
  bash.
- bash `CHANGES`, new features, between bash-4.4-beta2 and bash-4.4-rc2: "Using ${a[@]} or
  ${a[*]} with an array without any assigned elements when the nounset option is enabled no
  longer throws an unbound variable error."
  (<https://cgit.git.savannah.gnu.org/cgit/bash.git/plain/CHANGES?h=bash-4.4>, fetched
  today.) Before 4.4 the portable spelling is `${a[@]+"${a[@]}"}`: the `+` form expands to
  nothing when the array has no elements and is not an unbound-variable error.

## The decision

- The version refusal is removed. The `mapfile` becomes a read loop, the empty array is
  expanded with the `+` form, and a test keeps both constructs out of the installer: it fails
  if `mapfile`/`readarray` or a bare `"${IDE_HOOK_ARGS[@]}"` comes back. The macOS CI job
  under bash 3.2 is the run that verifies it; no bash 3.2 exists on the development machine.

Files: `install.sh`, `tests/test_the_installer_says_what_it_needs.py`,
`docs/research/2026-09-17-the-installer-runs-on-the-bash-macos-ships.md`.
