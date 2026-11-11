"""Every shell function the repository ships stays under the complexity ceiling.

The machine gate that measures an agent's edits skips `.sh`, and CI checked
`install.sh` only with `bash -n` and shellcheck, so installer functions grew to a
cyclomatic complexity of 7 to 14 unseen. lizard has no shell reader and reads a
`.sh` file as C, which misses `elif`, `until` and every `case` arm; this file
parses the shell with tree-sitter-bash, a dependency the code graph already
carries, and counts McCabe's decision points the way the shell spells them:
each `if`, `elif`, loop, `&&` and `||` adds one, and a `case` adds one per arm
except an explicit `*)` default (NIST SP 500-235, section 4.2).
See docs/research/2026-09-27-shell-functions-are-measured-too.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.shell_functions_stay_simple_helpers import (
    _ccn,
    _functions,
    _name,
    _shape_violations,
    _violations,
)

ROOT = Path(__file__).resolve().parents[1]
CODE_ZONES = ("scripts", "tests", "docs", "skills", "rules", "integrations", "benchmark")


def _shell_files() -> list[Path]:
    nested = [path for zone in CODE_ZONES for path in (ROOT / zone).rglob("*.sh")]
    return sorted([*ROOT.glob("*.sh"), *nested])


def test_the_installer_is_among_the_measured_files() -> None:
    assert ROOT / "install.sh" in _shell_files()


@pytest.mark.parametrize("path", _shell_files(), ids=lambda path: path.relative_to(ROOT).as_posix())
def test_every_shipped_shell_function_is_under_the_ceiling(path: Path) -> None:
    assert _violations(path.read_bytes()) == {}
    assert _shape_violations(path.read_bytes()) == []


@pytest.mark.parametrize("body", [
    "if a; then :; fi; if b; then :; fi; if c; then :; fi",
    "if a; then :; elif b; then :; fi",
])
def test_forbidden_shell_branch_shapes_are_detected(body):
    assert _shape_violations(f"bad() {{ {body}; }}".encode())


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('if a; then :; elif b; then :; fi', 3),
        ('until a; do :; done; while b; do :; done', 3),
        ('case "$1" in a) :;; b) :;; *) :;; esac', 3),
        ('case "$1" in a) :;; b|c) :;; esac', 3),
        ('a && b || c; [[ -n "$x" && -z "$y" ]]', 4),
        ('for x in 1 2; do :; done; for ((i=0; i<2; i++)); do :; done', 3),
        ('inner() { if a; then :; fi; }', 1),
    ],
)
def test_each_shell_decision_point_is_counted(body: str, expected: int) -> None:
    functions = _functions(f"outer() {{\n{body}\n}}\n".encode())
    assert _ccn(next(fn for fn in functions if _name(fn) == "outer")) == expected


def test_nesting_counts_blocks_inside_blocks() -> None:
    source = b"deep() {\n  for x in 1; do\n    while a; do\n      if b; then :; fi\n    done\n  done\n}\n"
    assert _violations(source) == {"deep": (4, 3)}
