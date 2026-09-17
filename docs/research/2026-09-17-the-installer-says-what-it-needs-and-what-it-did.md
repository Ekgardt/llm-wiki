# The installer says what it needs and what it did

Dated 2026-09-17. Findings I-A5, I-A8, I-A9, I-A11, I-A12 and I-A16 of the third audit
(medium to low, each confirmed by reading; the shell ones reproduced with stand-ins). The
research before the fix. They share one shape: the installer either stops without a usable
reason or reports something it did not check.

## What was found

- **A5.** `install.sh` uses `mapfile` and expands a possibly empty array under `set -u`.
  Stock macOS ships bash 3.2, where the first is `command not found` at step 3. Nothing
  checks the shell version.
- **A9.** Four `uv run` calls (`codex_memory.py hooks-state` / `config-state` in both
  installers) lack `--locked --no-sync`, as do the commands the summary prints. Every other
  call pins both.
- **A11.** `installed_memory_repair` accepts `partial` as adoptable (the adoption is
  resumable), but both installers adopt only `fresh` and `upgrade-required`; a vault whose
  adoption was interrupted is sent to the read-only check. The apply call discards its
  standard output, which is where the JSON report names the reason of a failure, so the
  "last lines" the installer promises to quote are empty.
- **A12.** Remote bootstrap runs `git init` before `git fetch`. A failed fetch leaves the
  directory, and the next attempt stops at "Remote install target already exists" with no
  way forward named.
- **A16.** `install.sh` prints "Claude Code: active automatic" whatever happened to the MCP
  registration: the flag is set to 1 and never changed. A `~/.claude.json` whose `llm-wiki`
  entry points at another vault passes the `grep` and is left as it is, silently.
- **A8.** A remote bootstrap checks out a detached commit; `self_update` skips a detached
  head by design, so such a vault never updates itself, and nothing says so.

## Practice on this date

- bash: "There is a new `mapfile' builtin to populate an array with lines from a given
  file. The name `readarray' is a synonym." — listed under the new features of bash-4.0
  (bash `NEWS`, <https://cgit.git.savannah.gnu.org/cgit/bash.git/plain/NEWS>, fetched
  today). That an empty array expanded as `"${a[@]}"` under `set -u` stopped being an error
  in bash 4.4 is from memory of the bash change log and was not re-read today; the guard
  therefore asks for 4.4, the stricter of the two bounds. bash 3.2 was not available here,
  so the installer was not rewritten to run on it — an unverifiable rewrite is worse than
  an honest refusal that names the remedy.
- uv: "when `uv run` is used, the project is locked and synced before invoking the
  requested command"; `--locked`: "If the lockfile is not up-to-date, uv will raise an
  error instead of updating the lockfile"; "The `dev` group is special-cased and synced by
  default." (<https://docs.astral.sh/uv/concepts/projects/sync/>, fetched today.) A bare
  `uv run` in a production install therefore pulls the dev group over the network.
- Debian Policy 6.2, on scripts that install: "If the first call failed, or aborted half
  way through for some reason, the second call should merely do the things that were left
  undone the first time" (<https://www.debian.org/doc/debian-policy/ch-maintainerscripts.html>,
  fetched today). That covers A11 and A12.

## The decision

- A5: a guard at the top of `install.sh`, before the first bash 4 construct runs, refuses
  bash older than 4.4 and names the remedy (`brew install bash`, then run the installer
  with that bash). The comparison is a function so a test can ask it about 3.2.
- A9: every `uv run` the installers execute or print carries `--locked --no-sync`; one test
  reads both files and holds that line.
- A11: `partial` joins the adoptable states in both installers; the apply call's output goes
  to the adoption log, so the quoted tail carries the reason.
- A12: the directory the bootstrap created is removed when the fetch or the checkout fails,
  so the same command can be run again; an existing complete checkout is named with the
  command that continues from it.
- A16: the state of the Claude MCP entry is read (absent, current, elsewhere, unreadable)
  by a function, never rewritten in place — `~/.claude.json` is Claude Code's live file —
  and the status line says "active automatic" only when the entry is current or was just
  registered.
- A8: the summary says that a pinned checkout is skipped by the nightly code update.
  Whether a remote bootstrap should follow a branch instead is the owner's decision (it
  trades the exact-commit pin for automatic updates) and is not taken here.

Files: `install.sh`, `install.ps1`, `tests/test_the_installer_says_what_it_needs.py`,
`docs/research/2026-09-17-the-installer-says-what-it-needs-and-what-it-did.md`.
