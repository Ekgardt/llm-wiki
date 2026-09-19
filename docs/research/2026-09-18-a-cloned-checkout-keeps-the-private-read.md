# A cloned checkout keeps the private read, and so does the interpreter that runs it

Dated 2026-09-18. Third audit, G-M8 point 3. The research before the fix.

Files: `scripts/workspace_revision.py`,
`tests/test_a_cloned_checkout_keeps_the_private_read.py`.

## What was found

- The private-index fast path — about 1 500 lines that verify a workspace revision against a
  sealed copy of git's index instead of re-hashing every file — is armed only when
  `_private_index_runtime_available()` is true, which asks `fcntl` for `F_ADD_SEALS`,
  `F_GET_SEALS` and the four seal bits. Verified on this machine: the interpreter `uv run`
  selects (CPython 3.14.7 from python-build-standalone, the one the project's own quick
  commands use) publishes **none** of those six names, while the system CPython 3.12.3 in
  `.venv` publishes all six. Under `uv run` the path is dead and about 45 tests skip with a
  reason that reads like a platform limitation.
- Armed, it then reads the repository's own `.git/config` and refuses anything it does not
  recognise. `_config_section_header` recognised `[core]`, `[extensions]` and `[user]` only,
  so the first `[remote "origin"]` line of any cloned repository made the whole config
  unsafe — verified against this vault's own `.git/config`, which `_safe_private_git_config`
  refused. The refusal happens after the index has been hashed, so such a checkout paid for
  the fast path and then took the slow one anyway.

## Practice on this date

- Linux's file seals are one ABI: `include/uapi/linux/fcntl.h` defines
  `F_LINUX_SPECIFIC_BASE` as 1024, `F_ADD_SEALS` as `F_LINUX_SPECIFIC_BASE + 9` and
  `F_GET_SEALS` as `+ 10`, with `F_SEAL_SEAL 0x0001`, `F_SEAL_SHRINK 0x0002`,
  `F_SEAL_GROW 0x0004` and `F_SEAL_WRITE 0x0008`
  (https://man7.org/linux/man-pages/man2/fcntl.2.html, "File seals", fetched 2026-09-18).
  The numbers do not vary by architecture; CPython simply may or may not publish them.
- `git-config(1)` on the sections in question: `remote.<name>.url`/`fetch` say where a
  branch is fetched from, `branch.<name>.remote`/`merge` say what `git pull` does
  (https://git-scm.com/docs/git-config, fetched 2026-09-18). Neither changes what git reads
  of the working tree, which is the only thing this private read depends on.

## The decision

- `_seal_constant(name)` answers with `fcntl`'s number when the build has it and with the
  Linux ABI number otherwise, on Linux only. Nothing else changes: the seals are applied and
  then read back (`applied & required == required`), so a wrong number can only disarm the
  path, never publish an unsealed index.
- `[remote "…"]` and `[branch "…"]` are read as sections whose settings cannot change what
  git reads. Any key in them is tolerated — editors add their own, such as
  `vscode-merge-base` — except `remote.<name>.promisor` and
  `remote.<name>.partialclonefilter`, because a promisor remote can make a read fetch
  objects. Every other section is still refused, and the refusal still means "use the exact
  read", never "answer anyway": `filter.*`, `credential.*`, `include*` and anything else
  unknown keeps the checkout on the slow path.
