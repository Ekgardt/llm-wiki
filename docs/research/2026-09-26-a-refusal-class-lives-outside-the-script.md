# A refusal class lives outside the script

Date: 2026-09-26. Audit 2026-09-26 item A-5 (B-37 was fixed only in-process).

## Fact
- The nightly runs `repository_index.py refresh-all` and `retire` as scripts
  (`scheduled_nightly.py` around 460-472). The script's handlers catch
  `RepositoryIndexRefused` as defined in `__main__`.
- `repository_worktrees.py` does `import repository_index as index` and raises
  `index.RepositoryIndexRefused` — the class of a second, separately imported
  copy of the module. The script's `except` does not match it: an opted-out
  checkout (`git config llmwiki.index false`, a documented opt-out) made
  `refresh-all` exit 1 with a traceback and skip every later checkout; `index` on
  an opted-out repository and `follow` on a non-git directory printed tracebacks
  instead of JSON (audit reproduction).

## Source (fetched 2026-09-26)
Python documentation, "`__main__` — Top-level code environment",
https://docs.python.org/3/library/__main__.html: "Python inserts an empty
`__main__` module in `sys.modules` at interpreter startup, and populates it by
running top-level code." A script therefore is `__main__`, and `import
repository_index` elsewhere creates a second module with its own classes.

## Decision
`RepositoryIndexRefused` moves into `repository_refusal.py`, a module that is never
run as a script, and `repository_index` imports it from there (re-exported under
the same name, so every caller keeps working). Every copy of `repository_index`
then shares one class object, and the CLI returns the refusal as JSON with exit 2.

## Uncertainty
Nineteen other scripts are both run and imported and define exception classes;
the same trap is possible where a sibling raises one of them while that script
runs as `__main__`. No such path was reported; they are not changed here.

## Files
- scripts/repository_refusal.py (new)
- scripts/repository_index.py
- tests/test_a_refusal_class_lives_outside_the_script.py
