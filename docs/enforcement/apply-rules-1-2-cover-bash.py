#!/usr/bin/env python3
"""Apply the rule 1 and 2 coverage change to the enforcement files.

Run as root. It copies both files aside, patches them, and then proves the gate
still answers. If any step fails, or the gate stops answering, both files go
back before it exits non-zero.

The rollback is the point, and it is what current practice asks for: validate
before the change takes effect, prove the thing still answers before trusting
it, and return to the last known good state automatically rather than as a
procedure someone remembers to follow. The gate's own docstring records what
happens without that — a self-recursive write once made every call raise, the
gate blocked all thirteen cases at once, and nobody could repair it. Research:
docs/research/2026-08-21-applying-a-policy-change-safely.md

    sudo /home/user/llm-wiki/.venv/bin/python \
        /home/user/llm-wiki/docs/enforcement/apply-rules-1-2-cover-bash.py
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
POLICY = ENFORCEMENT / "rules-policy.json"
GATE = ENFORCEMENT / "gate_pretooluse.py"
PYTHON = Path("/opt/claude-code-enforcement/venv/bin/python")
BACKUPS = Path("/etc/claude-code/backups")

RULE_IDS = (
    "R1-graph-consulted",
    "R1-graph-fresh",
    "R2-research-before-architecture",
)

ANCHOR = '''def _target_path(payload: dict) -> Path | None:
    raw = (payload.get("tool_input") or {}).get("file_path")
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw)
'''

REPLACEMENT = '''def _target_path(payload: dict) -> Path | None:
    raw = (payload.get("tool_input") or {}).get("file_path")
    if isinstance(raw, str) and raw:
        return Path(raw)
    targets = _bash_write_targets(payload)
    return targets[0] if targets else None


# Forms that can leave a file different from how they found it. A gate that
# only sees Edit and Write is a gate a shell heredoc walks around: on
# 2026-08-21 every edit of a long session arrived as `python3 - <<PY ... PY`
# inside Bash, so rules 1 and 2 never fired once across hundreds of changes.
_BASH_WRITE_FORMS = (
    r">>?\\s*(?!/dev/)\\S",
    r"(?<!\\w)tee(?!\\w)",
    r"(?<!\\w)(?:sed|perl)\\s+[^\\n;|]*(?<![\\w-])-i(?![\\w-])",
    r"(?<!\\w)(?:python3?|node|ruby|perl|deno|bun)\\s+[^\\n;|]*(?:(?<![\\w-])-c(?![\\w-])|<<)",
    r"(?<!\\w)(?:patch|git\\s+apply)(?!\\w)",
    r"(?<!\\w)(?:mv|cp|install|truncate|dd)(?!\\w)",
)

_PATH_IN_COMMAND = re.compile(r"[\\w./-]*[\\w-]+\\.[A-Za-z0-9]{1,8}")


def _bash_write_targets(payload: dict) -> list[Path]:
    """Repository files a shell command names, when the command can write.

    Undecidable in general: the path can be built at run time inside the
    heredoc. This catches the literal spelling, which is how a shell edit is
    nearly always written. A floor, not a proof, and stated as a limit for the
    same reason the graph check states its own.
    """
    command = _command_text(payload)
    if not command:
        return []
    if not any(re.search(form, command) for form in _BASH_WRITE_FORMS):
        return []
    base = Path(payload.get("cwd") or ".")
    found: list[Path] = []
    for match in _PATH_IN_COMMAND.finditer(command):
        candidate = Path(match.group(0))
        resolved = candidate if candidate.is_absolute() else base / candidate
        if resolved.is_file() and resolved not in found:
            found.append(resolved)
    return found
'''


def _fail(message: str) -> None:
    sys.stderr.write(f"не применено: {message}\n")


def _backup(stamp: str) -> dict[Path, Path]:
    """Copy both files aside, keeping mode and times, and answer where."""
    BACKUPS.mkdir(parents=True, exist_ok=True)
    saved: dict[Path, Path] = {}
    for path in (CHECKERS, POLICY):
        copy = BACKUPS / f"{path.name}.{stamp}"
        shutil.copy2(path, copy)
        saved[path] = copy
    return saved


def _restore(saved: dict[Path, Path]) -> None:
    for path, copy in saved.items():
        shutil.copy2(copy, path)


def _patched_checkers(text: str) -> str | None:
    """The checkers source with a Bash target resolver, or None if it will not fit."""
    if "_bash_write_targets" in text:
        _fail("checkers.py already carries the change")
        return None
    if text.count(ANCHOR) != 1:
        _fail(f"_target_path does not appear exactly once ({text.count(ANCHOR)})")
        return None
    return text.replace(ANCHOR, REPLACEMENT, 1)


def _entry_applies_to(entry: object, rule_id: str) -> list | None:
    """The applies_to list of one policy entry, or None when it has none."""
    if not isinstance(entry, dict):
        _fail(f"policy has no entry {rule_id}")
        return None
    applies = entry.get("applies_to")
    if isinstance(applies, list):
        return applies
    _fail(f"{rule_id} has no applies_to list")
    return None


def _entry_takes_bash(entry: object, rule_id: str) -> bool:
    """Add Bash to one entry's applies_to; False when the entry is not usable."""
    applies = _entry_applies_to(entry, rule_id)
    if applies is None:
        return False
    if "Bash" not in applies:
        applies.append("Bash")
    return True


def _patched_policy(document: dict) -> bool:
    """Add Bash to the three entries in place; False when one is missing."""
    entries = {entry.get("id"): entry for entry in document.get("entries", [])}
    return all(
        _entry_takes_bash(entries.get(rule_id), rule_id) for rule_id in RULE_IDS
    )


def _gate_answers() -> bool:
    """Whether the gate still returns a verdict instead of raising."""
    payload = json.dumps(
        {"tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": "true"}}
    )
    try:
        finished = subprocess.run(
            [str(PYTHON), str(GATE)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _fail(f"gate did not run: {error}")
        return False
    if finished.returncode not in {0, 2}:
        _fail(f"gate exited {finished.returncode}: {finished.stderr[:400]}")
        return False
    return True


def _write_both(checkers_text: str, policy_document: dict) -> None:
    CHECKERS.write_text(checkers_text, encoding="utf-8")
    POLICY.write_text(
        json.dumps(policy_document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _verify() -> bool:
    """Both files parse, and the gate still answers."""
    try:
        ast.parse(CHECKERS.read_text(encoding="utf-8"))
        json.loads(POLICY.read_text(encoding="utf-8"))
    except (SyntaxError, ValueError) as error:
        _fail(f"patched file does not parse: {error}")
        return False
    return _gate_answers()


def _prepared() -> tuple[str, dict] | None:
    """The patched contents of both files, or None when they cannot be built."""
    for path in (CHECKERS, POLICY, GATE, PYTHON):
        if not path.exists():
            _fail(f"missing {path}")
            return None
    checkers_text = _patched_checkers(CHECKERS.read_text(encoding="utf-8"))
    if checkers_text is None:
        return None
    policy_document = json.loads(POLICY.read_text(encoding="utf-8"))
    if not _patched_policy(policy_document):
        return None
    return checkers_text, policy_document


def main() -> int:
    prepared = _prepared()
    if prepared is None:
        return 1
    checkers_text, policy_document = prepared

    saved = _backup(time.strftime("%Y%m%dT%H%M%S"))
    try:
        _write_both(checkers_text, policy_document)
        verified = _verify()
    except Exception as error:  # noqa: BLE001 - a half-written gate is the danger
        _restore(saved)
        _fail(f"{type(error).__name__}: {error}; откачено")
        return 1
    if not verified:
        _restore(saved)
        sys.stderr.write("откачено, файлы вернулись к прежнему виду\n")
        return 1

    print(f"применено. копии: {saved[CHECKERS]} и {saved[POLICY]}")
    print("правила 1 и 2 теперь смотрят и на Bash")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
