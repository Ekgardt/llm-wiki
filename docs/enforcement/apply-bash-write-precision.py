#!/usr/bin/env python3
"""Make the Bash target resolver name the file a command actually writes.

The 2026-08-21 change let rules 1 and 2 see a shell edit. It found the target
by searching the whole command for a write form and then returning every path
in it, which is not a question text can answer: `grep policy.json > out.txt`
names two files and changes one. Within minutes it refused two ordinary reads,
and an over-firing gate is how a gate gets switched off.

This replaces that scan with the parser rule 4 already uses. Each group of
commands sharing a data path is judged on its own: a group that demonstrably
only reads contributes its redirect targets and nothing else.

It also closes a hole that scan was accidentally covering. `python3 - <<PY`
runs whatever arrives on stdin, and the read-only judgement counted it as
reading because no inline-code flag was present. Code on stdin is inline code
by another spelling, so an interpreter given no script file no longer reads.
That makes rule 4 stricter, which is why it is applied by the machine owner
running this, not as a step inside other work.

Same shape as the first script, for the reasons in
docs/research/2026-08-21-applying-a-policy-change-safely.md: copy aside,
patch, prove the gate still answers *and still decides correctly*, restore on
any failure.

    sudo python3 "$LLM_WIKI_ROOT/docs/enforcement/apply-bash-write-precision.py"

    ($LLM_WIKI_ROOT on this machine: /home/user/llm-wiki)
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ENFORCEMENT = Path("/etc/claude-code/enforcement")
CHECKERS = ENFORCEMENT / "checkers.py"
GUARD = ENFORCEMENT / "enforcement_guard.py"
POLICY = ENFORCEMENT / "rules-policy.json"
GATE = ENFORCEMENT / "gate_pretooluse.py"
PYTHON = Path("/opt/claude-code-enforcement/venv/bin/python")
BACKUPS = Path("/etc/claude-code/backups")

# The span of checkers.py this replaces, named by its first and last line.
CHECKERS_START = "# Forms that can leave a file different from how they found it."
CHECKERS_END = "def _command_text(payload: dict) -> str:"

CHECKERS_BLOCK = '''# Files a shell command can leave different from how it found them. A gate
# that only sees Edit and Write is a gate a shell heredoc walks around: on
# 2026-08-21 every edit of a long session arrived as `python3 - <<PY ... PY`
# inside Bash, so rules 1 and 2 never fired once across hundreds of changes.
#
# The first version searched the command text for a write form and then
# returned every path in it. `grep policy.json > out.txt` names two files and
# changes one, and it was refused as a write to the first. Text cannot say
# which file a verb acts on, so the parser rule 4 already uses answers first.
#
# It cannot answer alone. A heredoc body is not part of the parse, and the
# heredoc is where a shell edit puts its path. So the parse decides one thing
# only - whether every command here demonstrably reads - and the raw text is
# read back for paths when the answer is no.
_PATH_WORD = re.compile(r"[\\w./-]*[\\w-]+\\.[A-Za-z0-9]{1,8}")


def _bash_write_targets(payload: dict) -> list[Path]:
    """Files this command could leave changed, in the order they are named.

    Undecidable in general: a path built at run time inside a heredoc is
    invisible here, and only the first target reaches the caller. A command
    that writes also reports the files it merely reads, which costs a graph
    query rather than missing one. A floor, not a proof, and stated as a limit
    for the same reason the graph check states its own.
    """
    import shell_ast

    command = _command_text(payload)
    if not command:
        return []
    base = Path(payload.get("cwd") or ".")
    groups = shell_ast.parse_groups(command)
    found = _redirect_targets(groups, base)
    if _only_reads(groups):
        return found
    return list(dict.fromkeys(found + _target_paths(_PATH_WORD.findall(command), base, False)))


def _redirect_targets(groups: list | None, base: Path) -> list[Path]:
    """Files the shell itself would open for writing, created or not."""
    if groups is None:
        return []
    return _target_paths([word for group in groups for word in group.writes], base, True)


def _only_reads(groups: list | None) -> bool:
    """True when the parse shows every command here demonstrably cannot write.

    A command the parser could not read is not shown to be safe, so it is not
    read-only either.
    """
    if groups is None:
        return False
    return all(enforcement_guard.group_reads(group) for group in groups)


def _target_paths(words: list, base: Path, creating: bool) -> list[Path]:
    """Existing files among these words, and creatable ones when creating."""
    resolved = [_resolved_word(str(word), base) for word in words]
    return [path for path in resolved if _is_target(path, creating)]


def _resolved_word(word: str, base: Path) -> Path | None:
    if not _PATH_WORD.fullmatch(word):
        return None
    candidate = Path(word)
    return candidate if candidate.is_absolute() else base / candidate


def _is_target(path: Path | None, creating: bool) -> bool:
    """A file that exists, or one this command would create in a real directory."""
    if path is None:
        return False
    return path.is_file() or (creating and path.parent.is_dir())


'''

GUARD_EDITS = (
    (
        "import re\n\nimport shell_ast\n",
        "import re\nfrom functools import partial\n\nimport shell_ast\n",
    ),
    (
        "def _interpreter_reads(arguments: list[str]) -> bool:\n"
        "    return not (_flag_names(arguments) & INLINE_CODE_FLAGS)\n",
        "# Interpreters that take their program as a file argument. Given none they\n"
        "# run whatever arrives on stdin, which is inline code by another spelling:\n"
        "# `python3 - <<PY ... PY` was judged a reader while it could do anything.\n"
        "# sed and awk take their program as an argument instead, so the absence of\n"
        "# a file argument says nothing about them.\n"
        "STDIN_CODE_INTERPRETERS = frozenset(\n"
        '    {"python", "python3", "perl", "ruby", "node", "sh", "bash", "zsh"}\n'
        ")\n"
        "\n"
        "\n"
        "def _interpreter_reads(verb: str, arguments: list[str]) -> bool:\n"
        "    if _flag_names(arguments) & INLINE_CODE_FLAGS:\n"
        "        return False\n"
        "    if verb not in STDIN_CODE_INTERPRETERS:\n"
        "        return True\n"
        "    return _names_a_script(arguments)\n"
        "\n"
        "\n"
        "def _names_a_script(arguments: list[str]) -> bool:\n"
        '    """A non-flag argument, so the code is on disk rather than on stdin.\n'
        "\n"
        "    Narrow on purpose: `python3 -m pkg` names a module rather than a file\n"
        "    and still reads as one here. The gap is named rather than half-closed.\n"
        '    """\n'
        "    return any(not word.startswith(\"-\") for word in arguments)\n",
    ),
    (
        "SPECIAL_READERS.update({name: _interpreter_reads for name in INTERPRETERS})\n",
        "SPECIAL_READERS.update(\n"
        "    {name: partial(_interpreter_reads, name) for name in INTERPRETERS}\n"
        ")\n",
    ),
    (
        "def _command_reads(words: list[str]) -> bool:\n"
        "    unwrapped = _unwrap(words)\n"
        "    return bool(unwrapped) and _reads(unwrapped)\n",
        "def _command_reads(words: list[str]) -> bool:\n"
        "    unwrapped = _unwrap(words)\n"
        "    return bool(unwrapped) and _reads(unwrapped)\n"
        "\n"
        "\n"
        "def group_reads(group) -> bool:\n"
        '    """True when every command in the group demonstrably cannot write."""\n'
        "    return all(_command_reads(words) for words in group.commands)\n",
    ),
    (
        "    return not all(_command_reads(words) for words in group.commands)\n",
        "    return not group_reads(group)\n",
    ),
)

# What the gate must decide once this is applied. A gate that answers is not
# the same as a gate that answers correctly, and only the second is worth
# trusting: the first two of these were refused by the version being replaced.
EXPECTED = (
    ("true", 0, "a command that touches nothing"),
    (f"grep -c x {POLICY}", 0, "reading the policy with grep"),
    (f"grep -o x {POLICY} > /tmp/enforcement-probe.txt", 0, "a read redirected elsewhere"),
    (f"echo x >> {POLICY}", 2, "appending to the policy"),
    (f"python3 - <<'PY'\nopen('{POLICY}', 'a')\nPY", 2, "inline code on stdin"),
)


def _fail(message: str) -> None:
    sys.stderr.write(f"не применено: {message}\n")


def _backup(stamp: str) -> dict[Path, Path]:
    """Copy both files aside, keeping mode and times, and answer where."""
    BACKUPS.mkdir(parents=True, exist_ok=True)
    saved: dict[Path, Path] = {}
    for path in (CHECKERS, GUARD):
        copy = BACKUPS / f"{path.name}.{stamp}"
        shutil.copy2(path, copy)
        saved[path] = copy
    return saved


def _restore(saved: dict[Path, Path]) -> None:
    for path, copy in saved.items():
        shutil.copy2(copy, path)


def _span(text: str) -> tuple[int, int] | None:
    """Where the block being replaced starts and ends, or None if it is not there."""
    if text.count(CHECKERS_START) != 1 or text.count(CHECKERS_END) != 1:
        _fail("checkers.py does not carry the 2026-08-21 resolver exactly once")
        return None
    start = text.index(CHECKERS_START)
    end = text.index(CHECKERS_END)
    if start >= end:
        _fail("the resolver block is not where it was")
        return None
    return start, end


def _patched_checkers(text: str) -> str | None:
    """The checkers source with the parsed resolver, or None if it will not fit."""
    if "_redirect_targets" in text:
        _fail("checkers.py already carries the change")
        return None
    bounds = _span(text)
    if bounds is None:
        return None
    start, end = bounds
    return text[:start] + CHECKERS_BLOCK + text[end:]


def _applied_edit(text: str, before: str, after: str) -> str | None:
    if text.count(before) != 1:
        _fail(f"guard fragment appears {text.count(before)} times: {before[:60]!r}")
        return None
    return text.replace(before, after, 1)


def _patched_guard(text: str) -> str | None:
    """The guard source with stdin-fed interpreters counted as writers."""
    if "STDIN_CODE_INTERPRETERS" in text:
        _fail("enforcement_guard.py already carries the change")
        return None
    current = text
    for before, after in GUARD_EDITS:
        current = _applied_edit(current, before, after)  # type: ignore[arg-type]
        if current is None:
            return None
    return current


def _gate_verdict(command: str) -> int | None:
    """The gate's exit code for one command, or None when it did not answer."""
    payload = json.dumps(
        {"tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": command}}
    )
    try:
        finished = subprocess.run(
            [str(PYTHON), str(GATE)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _fail(f"gate did not run: {error}")
        return None
    if finished.returncode not in {0, 2}:
        _fail(f"gate exited {finished.returncode}: {finished.stderr[:400]}")
        return None
    return finished.returncode


def _decides_correctly() -> bool:
    """Every expected verdict, checked one at a time."""
    for command, expected, description in EXPECTED:
        actual = _gate_verdict(command)
        if actual is None:
            return False
        if actual != expected:
            _fail(f"{description}: expected exit {expected}, gate gave {actual}")
            return False
    return True


def _parses() -> bool:
    try:
        ast.parse(CHECKERS.read_text(encoding="utf-8"))
        ast.parse(GUARD.read_text(encoding="utf-8"))
    except SyntaxError as error:
        _fail(f"patched file does not parse: {error}")
        return False
    return True


def _write_both(checkers_text: str, guard_text: str) -> None:
    CHECKERS.write_text(checkers_text, encoding="utf-8")
    GUARD.write_text(guard_text, encoding="utf-8")


def _prepared() -> tuple[str, str] | None:
    """The patched contents of both files, or None when they cannot be built."""
    for path in (CHECKERS, GUARD, POLICY, GATE, PYTHON):
        if not path.exists():
            _fail(f"missing {path}")
            return None
    checkers_text = _patched_checkers(CHECKERS.read_text(encoding="utf-8"))
    guard_text = _patched_guard(GUARD.read_text(encoding="utf-8"))
    if checkers_text is None or guard_text is None:
        return None
    return checkers_text, guard_text


def _applied(prepared: tuple[str, str]) -> bool:
    """Write, then prove. Restores and answers False on any failure."""
    try:
        _write_both(*prepared)
        return _parses() and _decides_correctly()
    except Exception as error:  # noqa: BLE001 - a half-written gate is the danger
        _fail(f"{type(error).__name__}: {error}")
        return False


def main() -> int:
    prepared = _prepared()
    if prepared is None:
        return 1
    saved = _backup(time.strftime("%Y%m%dT%H%M%S"))
    if not _applied(prepared):
        _restore(saved)
        sys.stderr.write("откачено, файлы вернулись к прежнему виду\n")
        return 1
    print(f"применено. копии: {saved[CHECKERS]} и {saved[GUARD]}")
    print(f"проверено на {len(EXPECTED)} командах: чтение проходит, запись отвергается")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
