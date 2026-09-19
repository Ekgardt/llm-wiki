# A test of an optimisation must own the environment it asserts

Date: 2026-09-18
Files: `tests/test_workspace_revision.py`,
`tests/test_a_system_config_that_only_says_where_git_may_work.py`

## What was found

CI run 35382495391 still fails 67 tests in `tests/test_workspace_revision.py` on the two
Linux shard-3 jobs (105721706440, 105721706511). The set is **not** a new one: it is
byte-for-byte the same 67 test ids as run 35363057747, verified by diffing the two id
lists. The `[safe]` fix (`de5c037b`) was necessary and correct, and it moved nothing on
the runner, because the runner's `/etc/gitconfig` carries something else as well.

### What the runner's `/etc/gitconfig` actually holds

Two packages write to it, and both are on every hosted Ubuntu image:

1. `actions/runner-images`, `images/ubuntu/scripts/build/install-git.sh`, appends
   `[safe]` / `directory = *` — the section `de5c037b` taught the fence to tolerate.
2. The same image runs `apt-get install git-lfs`, and the git-lfs Debian package's
   `postinst` runs, verbatim from `git-lfs/git-lfs`'s `debian/postinst`:
   `git lfs install --skip-repo --system`.

`git lfs install --system` writes a `[filter "lfs"]` section into the system config,
setting `clean`, `smudge`, `process` and `required`.

A clean/smudge filter is the single most direct way a configuration can change what git
reads of a working tree — that is the whole purpose of one. So the semantics fence
refuses the file, `_private_raw_semantics_safe` returns `None`, and
`_private_git_state` restarts the verification without the fast path. That restart
discards the computed inventory too, which is why the three `test_inventory_hint_*` tests
and `test_verifier_reuses_computed_inventory_without_rescanning` fall over with it.

Reproduced here: pointing `_SYSTEM_GIT_CONFIG_PATHS` at a file holding the git-lfs filter
followed by the `[safe]` section gives **67 failed, 105 passed, 1 skipped**, and the 67
ids are identical to the CI 67 with no difference in either direction.

Checked and excluded along the way: the `/etc` walk itself works (`/etc/hosts` and
`/etc/hostname` read fine through the owned-file reader; `/etc/os-release` is refused only
because it is a symlink, which is the fence working as designed); the workflow sets no
`GIT_*` variable; and the CI traceback shows the plan armed with
`executable=PosixPath('/usr/bin/git')`, so the installation resolved.

## Which side is guilty

**The product is right.** Declining a fast path when the system configuration installs a
content filter is exactly the behaviour the fence exists to provide, and the answer the
caller receives is unchanged — the exact path produces it instead. Teaching the fence to
tolerate `filter.lfs.*` would be wrong: those settings really can change what git reports.

**The tests are guilty.** Sixty-seven of them assert `private_calls == 1` — that the
optimisation *fires* — while leaving the host's system git configuration unpinned. On any
machine with Git LFS installed system-wide, which is every hosted runner and a great many
developer machines, they fail while the product is behaving correctly.

The fixture in that file already knows this argument. Its own docstring says the private
path declines when a global configuration could change semantics, and that "the
optimization assertions passed only on a machine whose global configuration happened to be
empty" — which is why it pins `HOME` and `XDG_CONFIG_HOME`. The system configuration and
the system attributes file are the same class of host input, and they were simply never
pinned. So is the `GIT_*` selector environment, which
`_private_git_environment_overridden` refuses outright.

## Decision

The autouse fixture pins the remaining host inputs: `_SYSTEM_GIT_CONFIG_PATHS` and
`_SYSTEM_GIT_ATTRIBUTE_PATHS` point at files that do not exist, and every variable in
`_PRIVATE_GIT_SELECTOR_ENVIRONMENT`, plus any `GIT_CONFIG_KEY_n` / `GIT_CONFIG_VALUE_n`,
is removed. The nine names are imported from the product rather than restated, so a name
added there is covered here without anyone remembering to copy it.

This is not the "pin it and move on" shortcut that was rejected on 2026-09-18 while the
`[safe]` over-refusal was still live. That refusal was a real product defect, it is fixed,
and it is now held by a test of its own. What the fixture hides after this change is the
host, which is the only thing these tests should be blind to.

To keep the fence's strictness pinned by a test rather than by whatever `/etc` happens to
contain, `tests/test_a_system_config_that_only_says_where_git_may_work.py` gains the real
git-lfs system section as an explicitly refused case, beside the `[core] autocrlf` one. A
test also asserts the fixture actually neutralises the host, so the pinning cannot rot.

## Open, for the owner

Nothing in the product says *why* the fast path declined. It took a source dive through
two package build scripts to learn that a hosted runner disables it, and the only symptom
was 67 tests failing for what looked like the wrong reason twice in a row. A single
reported reason — in `doctor`, or in the verification result — would have made this a
one-line diagnosis, and would tell an operator whose machine has Git LFS that they are
paying for the exact path on every verification. That is a product change, not a CI fix,
so it is recorded here rather than made.

## Sources

- `actions/runner-images`, `images/ubuntu/scripts/build/install-git-lfs.sh`, fetched
  2026-09-18: the image installs git-lfs through `apt-get install git-lfs`.
- `git-lfs/git-lfs`, `debian/postinst`, fetched 2026-09-18: it runs
  `git lfs install --skip-repo --system`.
- `actions/runner-images`, `images/ubuntu/scripts/build/install-git.sh`, fetched
  2026-09-18: the `[safe]` / `directory = *` append.
- The local reproduction: the git-lfs filter plus the `[safe]` section in
  `_SYSTEM_GIT_CONFIG_PATHS` yields exactly the CI 67.
- Jobs 105721706440 and 105721706511 of run 35382495391, and the id diff against run
  35363057747.
