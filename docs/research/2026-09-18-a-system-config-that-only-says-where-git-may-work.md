# A system config that only says where git may work

Date: 2026-09-18
Files: `scripts/workspace_revision.py`,
`tests/test_a_system_config_that_only_says_where_git_may_work.py`

## What was found

CI run 35275051153 (head `f69f998c`) was a full success. Run 35363057747 (head
`801f6c0b`) fails, and 67 of its failures are in `tests/test_workspace_revision.py` on
the Linux runners alone — jobs 105658783855 (py3.14 shard 3) and 105658784011 (py3.10
shard 3). All 171 of those tests pass on this machine.

The private-index fast path is dead on every GitHub-hosted Linux runner, and it has been
since it was written. What changed on 2026-09-18 is that commit `fc91a047` stopped the
~45 of those tests from silently skipping, so the suite finally said so.

### The mechanism

`actions/runner-images` appends this to `/etc/gitconfig` when it builds the Ubuntu image
(`images/ubuntu/scripts/build/install-git.sh`, fetched 2026-09-18):

```bash
# Git version 2.35.2 introduces security fix that breaks action\checkout https://github.com/actions/checkout/issues/760
cat <<EOF >> /etc/gitconfig
[safe]
        directory = *
EOF
```

`/etc/gitconfig` is one of `installation.system_config_paths`. It is fenced in the config
group of `_semantics_groups` behind `_safe_ignored_git_config`, which allowed only blank
and comment lines and a `[user]` section carrying `email` or `name`:

```python
def _ignored_config_setting_allowed(line: str, in_user_section: bool) -> bool:
    if line.startswith("[") or not in_user_section:
        return False
```

`[safe]` is refused at that first line. From there:
`_add_semantics_file` → `_fence_semantics_groups` False → `_fence_all_semantics` False →
`_private_raw_semantics_safe` returns `None` → `_private_state_preconditions` →
`_private_git_state_from_index` → `_try_private_git_state` returns `None` →
`_private_git_state` raises `_RestartVerification(allow_private=False)`.

That restart is the frame in every CI traceback, and it also discards the computed
inventory, which is why `test_verifier_reuses_computed_inventory_without_rescanning` and
the three `test_inventory_hint_*` tests fall over as collateral.

The machine this runs on never reached `memfd_create` at all, which is why the failures
read `assert 0 == 1`, `assert 0 == 2` and `assert False` (an injection that never fired)
rather than anything about sealing.

### It is not the seal constants

The uv-managed CPython builds the runner installs genuinely lack the six `fcntl` seal
names — CI proves it directly with
`AttributeError: module 'fcntl' has no attribute 'F_SEAL_WRITE'` — but that is not what
breaks the path. `fc91a047` hardcoded the numbers, which are a Linux ABI, and read the
seals back after applying them. Deleting the six names from `fcntl` before importing
anything and running the whole file leaves 170 passed, 1 skipped, 1 failed — and the one
failure is a *test* that reads `fcntl.F_SEAL_WRITE` off the module, not the product.

`fc91a047` is not the defect. It is the flashlight: it un-skipped tests that had never
once run on a GitHub Linux runner, and they immediately reported a limitation the skip
had been hiding.

### Reproduced exactly

Pointing `_SYSTEM_GIT_CONFIG_PATHS` at a file holding exactly those two lines and running
the file on this machine gives **67 failed, 104 passed, 1 skipped** — and the 67 test ids
are identical to job 105658784011's 67, with no test in either set that is not in the
other.

## Decision

`safe.directory` and `safe.bareRepository` decide only whether git agrees to operate in a
repository. Neither can change what git reads of the working tree, its ignore rules, its
attributes or its index — which is the only thing this fence exists to protect. Refusing
them is the same class of over-refusal `fc91a047` already fixed for `[remote "…"]` and
`[branch "…"]`.

`_ignored_config_line_step` now tracks *which* inert section is open rather than a
boolean "are we in `[user]`", and `_INERT_EXTERNAL_CONFIG_SECTIONS` names the two
sections and the exact keys each may carry. A `safe.*` key that is not one of those two
is still refused, and every semantics-changing setting is refused exactly as before — the
existing guard `test_private_index_declines_global_and_system_config_files[system]`,
which writes `[core] autocrlf = false` into the system config and demands the private
path is not used, still passes. The file is still fenced by digest and identity and still
re-proved after status.

With the fix, the runner-shaped reproduction gives **171 passed, 1 skipped**, and so does
a plain run.

What is deliberately **not** done: pinning `_SYSTEM_GIT_CONFIG_PATHS` in the autouse
`_isolated_global_git_configuration` fixture. That would make CI green without fixing
anything and would re-hide exactly what these tests just found. If the suite should later
be made deterministic against whatever a host's `/etc/gitconfig` holds, that belongs
alongside this fix, never instead of it.

## Open, for the owner

The 67 failures were only visible because `fc91a047` un-skipped them. A skip that hides a
platform's whole optimisation is indistinguishable, in a green run, from a platform that
does not need it. Worth a separate look: whether a skipped optimisation should be
reported by `doctor` on the machine it is skipped on.

## Sources

- `actions/runner-images`, `images/ubuntu/scripts/build/install-git.sh`, fetched
  2026-09-18 from `raw.githubusercontent.com/actions/runner-images/main/...`, quoted
  verbatim above.
- The local reproduction: `_SYSTEM_GIT_CONFIG_PATHS` pointed at those two lines yields
  67 failures whose ids match job 105658784011's 67 exactly.
- Job logs 105658783855 and 105658784011 of run 35363057747, including the
  `_RestartVerification` frame at `scripts/workspace_revision.py` and the
  `AttributeError` on `fcntl.F_SEAL_WRITE`.
- `git log -1 fc91a047` and CI run 35275051153, the last full success before it.
