# The vault and state roots are read at call time, in one place

Date: 2026-11-10.

## The defect

Every script copied `LLM_WIKI_ROOT` and `LLM_WIKI_STATE_ROOT` into module constants
when it was first imported (`ROOT`, `STATE_ROOT`, `KNOWLEDGE`, `LOCK_FILE`, ...). The
consequences:

- Modules resolved the vault in different ways. The queue and the transactions took
  the directory of `__file__`, while `memory_state` resolved the main checkout of a
  Git worktree. Run from a worktree, two writers wrote to two different vaults.
  Confirmed on the base commit.
- `LLM_WIKI_STATE_ROOT=""` resolved to the current directory, not to the vault.
- A test that set the variables reached only modules imported after it. Tests
  patched constants module by module, and every new constant needed a new patch. A
  forgotten patch let a test write into the real vault.

## Sources

1. pytest issue #2229, <https://github.com/pytest-dev/pytest/issues/2229>.
   `monkeypatch.setenv` cannot change a value a module already read at import time.
   The setting reaches only code that reads the environment afterwards.
2. Adam Johnson, "How to Mock Environment Variables in pytest",
   <https://adamj.eu/tech/2020/10/13/how-to-mock-environment-variables-with-pytest/>.
   The post covers `importlib.reload` and a subprocess as workarounds for import-time
   reads. Reloading leaves `from`-imported copies stale, so the post calls it a
   compromise, not a pattern.
3. The Twelve-Factor App, "III. Config", <https://12factor.net/config>. Configuration
   that varies between deploys belongs in the environment, not in constants in code.
4. konfetti, <https://github.com/kiwicom/konfetti>. A Python settings library built on
   lazy evaluation: settings are read when the application needs them, not when they
   are declared. Not verified: whether konfetti re-reads a variable on every access
   or caches it after the first read.

## Alternatives considered

- **Keep the constants and patch them in tests.** This is the status quo. Every
  constant is one more patch, production still has the worktree divergence, and a
  missed patch is silent.
- **Reload modules after setting the variables.** Copies taken with `from x import y`
  stay stale (source 2). Reloading also re-runs import side effects.
- **Adopt a settings library.** Adds a dependency to fix about a dozen derivations.
  The repository already has `scripts/settings.py` for limits, and the roots are
  needed before it can be read.
- **One stdlib module that reads the environment on every call (chosen).** The cost is
  a dictionary lookup and a `Path.resolve()` per call. That cost is invisible next to
  the file I/O every caller then does. The only cached value is the Git probe of the
  checkout, because a running process does not move its checkout.

## Decision

`scripts/llm_wiki/runtime_roots.py` owns:

- `vault_root()` and `state_root()`;
- `state_root_for(vault)`;
- the derived `knowledge/` paths: `notes`, `daily`, `projects`, `index.md`;
- the derived `run/` and `logs/` paths.

An unset or empty variable names nothing. Every module calls these functions. The
removed module constants are not kept as aliases: they would be the stale copies this
change exists to remove.

The private vault log path is `vault_log.vault_log_path(vault)`. It is a pure function,
because `vault_log` belongs to a module declared to import only the standard library.

## Protection against recurrence

`tests/test_roots_are_read_at_call_time.py` parses every script. It fails on a module
that reads a root at import time, whether directly, through `os.environ`, or through
one of its own functions that does. It skips `if __name__ == "__main__"` blocks. The
original defect is checked directly too: `tests/test_structure.py` covers an unset and
an empty state root.

An import inside a function runs only when the function does. Two such imports of the
removed `compile_pipeline.ROOT` in `compile_controller` survived the change and failed
only when a compile was published. `tests/test_every_imported_name_is_bound.py` parses
every script and fails on a first-party `from x import y`, at any scope, whose module
does not bind `y`. Run against the earlier `compile_controller`, it names both imports.

## Trade-offs

- Code that cached a path across calls now recomputes it on every call.
- Tests now set environment variables, not module attributes.
- Changing a variable while the process runs moves later calls to the new root. This
  is the intended contract, and no production caller does it.
