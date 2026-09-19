# A refusal names its component and its rule, and three neighbours of the same shape

Dated 2026-09-18 (the work began on 2026-09-17 and the clock rolled over mid-task).
Finding K-B22 of the third audit, the parts that live in the LSP security and launch
modules. The research before the fix.

Files: `scripts/lsp_security.py`, `scripts/lsp_launch_package.py`,
`tests/test_a_refused_path_names_its_component_and_its_rule.py`,
`tests/test_lsp_security.py`.

## What was found

- **Windows file-name rules are enforced on Linux and macOS.**
  `_unportable_component_text` refuses a component that ends in `.` or a space, contains
  any of `<>:"|?*`, or whose stem is a reserved DOS device name. A file called `aux.py` or
  `notes:draft.md` is an ordinary file on Linux and macOS. The refusal read "repository
  source path contains an unsafe component", naming neither the component nor the rule.
- **`normalize_provider_uri` ends in `except Exception: return None`**
  (`lsp_security.py`). Every location a language server returns passes through it
  (`code_navigation.py:511,629`, `pyright_session.py:2643,3673,4077`). A defect inside the
  module is indistinguishable from "this location is outside the repository".
- **`_resolve_posix` / `_resolve_windows` are not dead — they do not exist.** `grep` over
  `scripts/` finds no such names. The functions at those lines are `_access_posix` and
  `_access_windows`, both called from `_access_repository_source`. Not a defect.
- **`lsp_launch_package._empty_tree` runs outside the caller's `try`.**
  `create_launch_tree` wraps `_populate` and `verify_sealed_entry` in a `try` whose
  `except` calls `remove_launch_tree`, but the tree is built before that `try` begins. If
  the second `os.mkdir` in `_created_directories` fails — a full disk, a name already
  taken — the root that was already made stays inside `run/lsp/<nonce>/` forever.
- **One more, found while testing:** `test_redaction_bounds_every_stage_before_contractions_and_output`
  ends in `assert elapsed < 1.0`. On this machine, with several suites running at once,
  the same unchanged redactor took 1.15 s, 1.35 s and 1.25 s (measured today against
  commit `2d745a3`), so the test failed for load, not for an unbounded stage.

## Practice on this date

- The naming rules being enforced are Windows', not POSIX'. "Use any character in the
  current code page for a name, including Unicode characters and characters in the
  extended character set (128–255), except for the following: The following reserved
  characters: `<` (less than) `>` (greater than) `:` (colon) `"` (double quote) `/`
  (forward slash) `\` (backslash) `|` (vertical bar or pipe) `?` (question mark) `*`
  (asterisk)"; "Do not use the following reserved names for the name of a file: CON, PRN,
  AUX, NUL, COM1, COM2, … LPT9, LPT¹, LPT², and LPT³."; "Do not end a file or directory
  name with a space or a period. Although the underlying file system may support such
  names, the Windows shell and user interface does not." (Microsoft, *Naming Files, Paths,
  and Namespaces*,
  <https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file>, fetched today.)
  POSIX names a much smaller set: a pathname component may hold any byte except `/` and
  NUL.
- **The product already decided this, and the decision is tested.**
  `tests/test_code_navigation.py::test_public_navigation_paths_require_canonical_nfc_posix_relative_text`
  lists `pkg/CON.py`, `pkg/con.txt`, `pkg/COM1.py`, `pkg/api.py:stream`, `pkg/trailing.`
  and `pkg/trailing ` among the paths every public navigation entry point must refuse, on
  every platform. One repository-relative path is one key everywhere, and the same rule on
  every host is what makes that true.
- **The broad `except` is also a decision, and it is tested.**
  `tests/test_lsp_security.py::test_provider_failures_never_raise_log_or_echo_raw_uri`
  injects a `RuntimeError` carrying the raw URI and requires `normalize_provider_uri` to
  answer `None` and to log nothing. `uri` is untrusted text a language server sent; what
  must not happen is that text escaping in a traceback or a log line. Narrowing the
  `except` to `(OSError, ValueError)` makes a `RuntimeError` carrying that URI propagate —
  exactly what the test forbids.
- Where the cost of that swallow actually falls was measured: with the `except` narrowed,
  seven of the nine navigation cases that already fail closed keep failing closed, so the
  only behaviour the narrowing changes is whether a defect escapes with provider text
  attached.

## The decision

1. The Windows file-name rules stay on every platform — one path, one key — and the
   refusal now names the component and the rule it broke: `repository source path
   component 'aux.py' is a reserved Windows device name (a Windows rule)`. That is the
   half of the finding that was a defect: a refusal that explains nothing.
2. `normalize_provider_uri` keeps its broad `except`, now with the reason written beside
   it and the test that pins it named. The diagnosability cost is real and is recorded
   here rather than traded for a way to leak untrusted provider text.
3. `_created_directories` takes back what it made when it cannot finish, so no partial
   launch tree is left in the owner's scratch.
4. `_resolve_posix` / `_resolve_windows`: no change, they are not there.
5. The stopwatch in the redaction-bounds test is removed. What "bounded" means there is
   the size each stage is handed, which the test already asserts against a 256 KiB
   ceiling; the wall clock only added a way to fail on a loaded machine.

Rejected: applying the Windows rules only on Windows (it would flip a tested product rule
about path keys, for a filename that is rare); narrowing the provider `except` (see
above).
