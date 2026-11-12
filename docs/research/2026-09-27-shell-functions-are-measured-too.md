# Shell functions are measured too

Date: 2026-09-26 (audit of 2026-09-27, law 5).

## What was true

The machine gate that measures every edit covers 26 languages and skips `.sh`.
CI checked `install.sh` with `bash -n` and shellcheck only. Five installer
functions had grown past the ceiling of 5, counted the way the section below
defines: `stop_test_child` 14 (nesting 3), `protect_push_urls` 10,
`configure_codex_mcp` 7, `fetch_pinned_checkout` 7, `stop_test_timer` 7.

## Sources

1. lizard README, https://github.com/terryyin/lizard (read 2026-09-26). The
   supported-language list is "C# (C Sharp), C/C++ (works with C++14), Erlang,
   Fortran, GDScript, Golang, Java, JavaScript (With ES6 and JSX), Kotlin, Lua,
   Objective-C, Perl, PHP, PL/SQL, Python, R, Ruby, Rust, Scala, Solidity,
   Structured Text (St), Swift, TTCN-3, TypeScript (With TSX), VueJS, Zig" --
   no shell. lizard 1.24.0 reads an unknown extension with
   `(get_reader_for(filename) or CLikeReader)` (`lizard.py:615`), so a `.sh`
   file is measured as C: `elif`, `until` and `case` arms are not decisions to it.
   On the old `install.sh` it gave no warning for `configure_codex_mcp` while the
   function had three `case` outcomes, two `if` and one `||`.
2. NIST SP 500-235, McCabe and Watson, "Structured Testing", section 4.2,
   https://www.mccabe.com/pdf/mccabe-nist235r.pdf (read 2026-09-26): "An 'if'
   statement, 'while' statement, and so on are binary decisions, and therefore
   add one to complexity. Boolean operators add either one or nothing to
   complexity, depending on whether they have short-circuit evaluation
   semantics"; for a multiway decision "The number added is one less than the
   number of edges out of the decision node"; "An implicit default or
   fall-through branch, if specified by the language, must be taken into
   account". On limits: "The original limit of 10 as proposed by McCabe has
   significant supporting evidence" -- the owner's law sets 5, stricter.
3. shellmetrics 0.5.0, https://github.com/shellspec/shellmetrics (read
   2026-09-26): measures "NLOC - Non-comment Lines of Code, LLOC - Logical Lines
   of Code, CCN - Cyclomatic Complexity number" for "bash, mksh, yash, zsh"; its
   flags are "`-s/--shell`, `--[no-]color`, `--csv`, `-p/--pretty`,
   `-d/--debug`, `-v/--version`, and `-h/--help`" -- no threshold, no failing
   exit code. Run here it gave the old `protect_push_urls` 3, for a function
   with a loop, an `if` and seven `||`: it does not count `||` after a
   line continuation.
4. ShellCheck README, https://github.com/koalaman/shellcheck (read
   2026-09-26): its goals are syntax issues, "intermediate level semantic
   problems" and "subtle caveats, corner cases and pitfalls"; it does not
   measure complexity.

## Alternatives

- lizard with its C reader: the same tool as the machine gate, but it does not
  know the shell and under-counts `case` and `elif`, which is how
  `configure_codex_mcp` passed it at 7.
- shellmetrics: shell-aware, but no CI exit code, not on PyPI (a pinned download
  in CI and a vendored copy for local runs), and it under-counts `||`.
- A counter on tree-sitter-bash (chosen): the grammar is already a dev and
  `code-graph` dependency (`scripts/code_extractor.py:47`), so nothing new is
  installed; the grammar names `elif_clause`, `case_item`, `while_statement`
  (which carries `until` too), `for_statement`, `c_style_for_statement` and the
  `&&`/`||` tokens, so each of NIST's decisions is one node type. It also
  measures the law's nesting of `if`/`for`/`while`, which neither tool does for
  shell.

## Decision

`tests/test_shell_functions_stay_simple.py` measures every tracked `.sh` file:
each `if`, `elif`, loop, `&&` and `||` adds one; a `case` arm adds one unless it
is a lone `*)` default (NIST's implicit-default rule); nesting of
`if`/`for`/`while` stays at 2 or less. Unit cases pin each counting rule, so a
counter that stopped seeing a construct fails. The five functions were split
into steps of one purpose each; the installer's behaviour is unchanged, with one
exception: `configure_codex_mcp` no longer reports success when copying the
config to `config.bak` fails (it returned 0 before, because the function runs
inside `if` where `set -e` is off).

## Trade-offs

- The counter is ours, about forty lines; its rules are pinned by tests and by
  the NIST text above, not by a third-party tool's version.
- Until 2026-11-11 the top-level body of `install.sh` was not a function and was
  not measured; shellmetrics put its decision count at 63. That reading is
  superseded below.

## 2026-11-11: the script body is one more unit

Law 5 applies to every written or changed unit of code, and the statements
outside every function are such a unit: the installer's body had reached CCN 112,
nesting 3, 39 `if` and 5 `elif` (98, 3, 34 and 5 on base 647f8148), measured with
the counter above applied to the parse tree's root without its functions.
`_script_body_violations` in `tests/shell_functions_stay_simple_helpers.py` now
measures that body under the same ceiling, nesting limit, `if` count and `elif`
ban, and `test_every_shipped_script_body_is_under_the_ceiling` applies it to every
shipped `.sh` file. Four planted cases in `test_branching_outside_functions_is_measured`
prove it catches each shape. `install.sh` was split into one function per step;
its body is now `main "$@"` (CCN 1, nesting 0), and every function stays within
the thresholds.

The PowerShell guard (`tests/powershell_functions_stay_simple_helpers.py`) now
reports each script's body too, as a record named `(script body)`. Three bodies
were over the ceiling on the base; this branch had changed all three scripts:

| Script | Base 647f8148 | Before this change | Now |
|---|---|---|---|
| `install.ps1` | CCN 94, 62 `if`, nesting 3, 5 `elseif` | CCN 103, 72 `if`, nesting 3, 4 `elseif` | CCN 86, 57 `if`, nesting 3, 3 `elseif` |
| `scripts/install-scheduled-tasks.ps1` | CCN 21, 18 `if`, nesting 3 | same | CCN 2, 1 `if`, nesting 1 |
| `scripts/codex-memory-wrapper.ps1` | CCN 8, 5 `if`, nesting 2, 1 `elseif` | same | CCN 2, 1 `if`, nesting 1 |

The owner deferred splitting `install.ps1`'s body on 2026-11-11 to task 178,
item 2: Pester tests first, then the split. The same decision says this branch
must not make the body worse. The branch's own additions to the body (flag
forwarding on re-run, choosing the PowerShell host, finding Python 3.14, the
model choice, the Codex advice state and the managed-Pyright check) were therefore
moved into functions within the law-5 thresholds, and
`tests/test_the_windows_installer_forwards_its_flags.py` runs each of them in real
PowerShell. `test_no_script_body_grows_past_its_held_level` holds that body at its
current level and every other body to the law-5 ceiling. It fails when the held
body improves without its entry being lowered, so the ratchet only tightens; the
entry is removed once the body measures within the ceiling.

The other two bodies were split into functions, one per mode or lookup step, and
are held to the ceiling like every other script. Splitting the scheduler script
exposed a defect present on the base: it decided it
was dot-sourced from `$MyInvocation.CommandOrigin -eq 'Internal'`, but a script
with mandatory parameters run by `pwsh -File` reports `Internal` too (checked with
PowerShell 7.5.4), so `-Status` returned instead of exiting and ended with code 0
when the tasks were missing. The script now asks `$MyInvocation.InvocationName -eq '.'`,
which is `.` only for dot-sourcing. `tests/test_the_scheduled_task_installer_answers_each_mode.py`
runs the whole script as a child process in each mode; its status case fails on
the base script with exit 0 instead of 1.
