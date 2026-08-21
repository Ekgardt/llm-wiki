#!/usr/bin/env python3
"""Turn the turn-end gate into something that actually enforces, and close two
holes the earlier widening left.

Research: docs/research/2026-08-21-hardening-the-turn-end-guard.md

Four changes, all deterministic, none of which asks the model anything.

1. The turn-end gate refused once and then permitted unconditionally, twice
   over: `main` returned early on `stop_hook_active`, and it reset the turn
   window even when refusing, so the retry evaluated an empty window. "Run
   something that could fail before declaring the work done" is a renewal
   obligation, and a monitor enforces one only by refusing again. Now it does.
2. Bounded, and the give-up is recorded. After MAX_TURN_END_BLOCKS consecutive
   refusals - or if the marker cannot be written at all - the gate permits, but
   writes a `yield` decision naming the undischarged obligation and says so on
   stderr. A bypass that leaves evidence is a different thing from a silent one.
3. Rules 1 and 2 were widened to Bash earlier today; the new-module trigger was
   not, so a module created by a redirect or a heredoc did not count as one.
4. Rule 2 was satisfied by any file under the research directory carrying
   today's date. It now has to cite a source and say something.

Same shape as the two scripts before it: copy aside, patch, prove the gate
still decides correctly, restore everything on any failure.

    sudo python3 /home/user/llm-wiki/docs/enforcement/apply-guard-hardening.py
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
GATE_STOP = ENFORCEMENT / "gate_stop.py"
STATUS = ENFORCEMENT / "status.py"
POLICY = ENFORCEMENT / "rules-policy.json"
GATE = ENFORCEMENT / "gate_pretooluse.py"
PYTHON = Path("/opt/claude-code-enforcement/venv/bin/python")
BACKUPS = Path("/etc/claude-code/backups")

TOUCHED = (CHECKERS, GATE_STOP, STATUS)

# A file already carrying its change is left alone; all three must be unpatched
# for the run to start, so a half-applied state cannot be reached by rerunning.
MARKERS = {
    CHECKERS: "MIN_RESEARCH_CHARS",
    GATE_STOP: "MAX_TURN_END_BLOCKS",
    STATUS: "_recent_yields",
}

_STOP_MARKER = (
    '''def _turn_start(session: str) -> float:
    try:
        payload = json.loads(common.TURN_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    if payload.get("session") != session:
        return 0.0
    value = payload.get("at")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _record_turn_end(session: str) -> None:
    payload = {"session": session, "at": time.time()}
    try:
        common.TURN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        common.TURN_MARKER.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return
''',
    '''# Refusing once and then permitting enforces nothing. Truncation automata
# enforce safety properties - "nothing bad happens"; "something that could have
# failed must run before the work is declared done" is a renewal obligation,
# and the only way a monitor enforces one is by refusing again until it is
# discharged. See docs/research/2026-08-21-hardening-the-turn-end-guard.md in
# the llm-wiki repository.
#
# Bounded, because an obligation that cannot be discharged would otherwise
# wedge the session and the runtime would cut the hook off regardless. On the
# bound the gate permits and records a `yield` naming what stayed undischarged.
MAX_TURN_END_BLOCKS = 3


def _marker(session: str) -> dict:
    """This session's turn marker, or an empty one."""
    try:
        payload = json.loads(common.TURN_MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("session") != session:
        return {}
    return payload


def _turn_start(session: str) -> float:
    value = _marker(session).get("at")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _blocks_so_far(session: str) -> int:
    value = _marker(session).get("blocks")
    return value if isinstance(value, int) and value > 0 else 0


def _write_marker(session: str, at: float, blocks: int) -> bool:
    """Persist the marker. False when it could not be written."""
    payload = {"session": session, "at": at, "blocks": blocks}
    try:
        common.TURN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        common.TURN_MARKER.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return False
    return True


def _record_turn_end(session: str) -> None:
    """The turn closed: the next window starts here and the count resets."""
    _write_marker(session, time.time(), 0)


def _record_block(session: str) -> int | None:
    """Count one refusal without moving the window, or None if it cannot be.

    The window must not move on a refusal. It used to, and the retry then
    evaluated an empty window and passed - the second of the two reasons this
    gate never enforced anything.
    """
    blocks = _blocks_so_far(session) + 1
    if not _write_marker(session, _turn_start(session), blocks):
        return None
    return blocks
''',
)

_STOP_MAIN = (
    '''def main() -> int:
    payload = common.hook_input()
    session = payload.get("session_id", "")
    if payload.get("stop_hook_active"):
        _record_turn_end(session)
        return 0
    records = _entries_since(_turn_start(session))
    reason = evaluate(records)
    _record_turn_end(session)
    if reason is None:
        common.audit("allow", tool="turn-end", subject=session, rule_id=None)
        return 0
    common.audit("refuse", tool="turn-end", subject=session, rule_id="R3-turn-end", detail=reason)
    print(reason, file=sys.stderr)
    return 2
''',
    '''def main() -> int:
    payload = common.hook_input()
    session = payload.get("session_id", "")
    reason = evaluate(_entries_since(_turn_start(session)))
    if reason is None:
        _record_turn_end(session)
        common.audit("allow", tool="turn-end", subject=session, rule_id=None)
        return 0
    return _refuse_or_yield(session, reason)


def _refuse_or_yield(session: str, reason: str) -> int:
    """Refuse while the obligation stands; give up loudly at the bound."""
    blocks = _record_block(session)
    if blocks is None or blocks > MAX_TURN_END_BLOCKS:
        return _yield(session, reason, blocks)
    common.audit(
        "refuse", tool="turn-end", subject=session, rule_id="R3-turn-end", detail=reason
    )
    print(
        f"{reason}\\nRefusal {blocks} of {MAX_TURN_END_BLOCKS} for this turn.",
        file=sys.stderr,
    )
    return 2


def _yield_cause(blocks: int | None) -> str:
    if blocks is None:
        return "the turn marker could not be written"
    return f"{blocks - 1} refusals did not discharge it"


def _yield(session: str, reason: str, blocks: int | None) -> int:
    """Permit an undischarged turn, and record that this is what happened."""
    cause = _yield_cause(blocks)
    _record_turn_end(session)
    common.audit(
        "yield",
        tool="turn-end",
        subject=session,
        rule_id="R3-turn-end",
        detail=f"{cause}\\n{reason}",
    )
    print(
        f"TURN-END GATE YIELDED: {cause}.\\nThe obligation below was never met, "
        f"and the yield is recorded in {common.AUDIT_LOG}.\\n{reason}",
        file=sys.stderr,
    )
    return 0
''',
)

_STOP_DOCSTRING = (
    """Limit: the runtime overrides a Stop hook after 8 consecutive blocks. It is a
strong brake, not a wall.
""",
    """Limit: the refusal repeats while the obligation stands, up to
MAX_TURN_END_BLOCKS, and then permits and records a `yield`. The runtime also
cuts a Stop hook off after 8 consecutive blocks. It is a strong brake, not a
wall.
""",
)

_NEW_MODULE = (
    '''def _is_new_module(target: Path, payload: dict) -> bool:
    if payload.get("tool_name") != "Write":
        return False
    return target.suffix == ".py" and not target.exists()
''',
    '''# Rules 1 and 2 were widened to Bash on 2026-08-21 and this trigger was not,
# so a module created by a redirect or a heredoc was not a new module. Which
# tool creates the file does not change what creating it means.
_CREATING_TOOLS = frozenset({"Write", "Bash"})


def _is_new_module(target: Path, payload: dict) -> bool:
    if payload.get("tool_name") not in _CREATING_TOOLS:
        return False
    return target.suffix == ".py" and not target.exists()
''',
)

# A command that is not demonstrably read-only can create a file, so a path it
# names counts even when nothing is there yet. Without this the new-module
# trigger above still cannot see a heredoc that writes a module for the first
# time, because the target would not resolve.
_CREATING_TARGETS = (
    "    return list(dict.fromkeys(found + _target_paths(_PATH_WORD.findall(command), base, False)))\n",
    "    named = _target_paths(_PATH_WORD.findall(command), base, True)\n"
    "    return list(dict.fromkeys(found + named))\n",
)

_RESEARCH = (
    '''def _research_today(root: Path, policy: dict) -> bool:
    directory = root / common.policy_settings(policy).get("research_dir", "docs/research")
    if not directory.is_dir():
        return False
    today = time.strftime("%Y-%m-%d")
    return any(_dated_today(path, today) for path in directory.rglob("*") if path.is_file())


def _dated_today(path: Path, today: str) -> bool:
    if today in path.name:
        return True
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return False
    return today in head
''',
    '''# What makes an artifact research rather than a file with today's date on it.
# Deliberately not subject-matching: requiring the note to name the exact file
# being changed would refuse honest research, and an over-firing gate is how a
# gate gets switched off.
MIN_RESEARCH_CHARS = 400
SOURCE_URL = re.compile(r"https?://\\S{4,}")
MAX_RESEARCH_READ = 20000


def _research_today(root: Path, policy: dict) -> bool:
    directory = root / common.policy_settings(policy).get("research_dir", "docs/research")
    if not directory.is_dir():
        return False
    today = time.strftime("%Y-%m-%d")
    return any(_is_research(path, today) for path in directory.rglob("*") if path.is_file())


def _is_research(path: Path, today: str) -> bool:
    """Dated today, long enough to say something, and citing a source."""
    text = _read_head(path)
    if not _dated_today(path, today, text):
        return False
    return len(text) >= MIN_RESEARCH_CHARS and bool(SOURCE_URL.search(text))


def _read_head(path: Path) -> str:
    """Enough of the file to reach a Sources section at the end of a note."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_RESEARCH_READ]
    except OSError:
        return ""


def _dated_today(path: Path, today: str, text: str) -> bool:
    return today in path.name or today in text[:2000]
''',
)

_RESEARCH_MESSAGE = (
    '''        f"Research current best practice, versions, deprecations, security and "
        f"alternatives, then record it under {directory}/ dated today.\\n"
''',
    '''        f"Research current best practice, versions, deprecations, security and "
        f"alternatives, then record it under {directory}/ dated today.\\n"
        "The note must cite at least one source and say something: a dated stub "
        "does not satisfy this.\\n"
''',
)

_STATUS_COUNTS = (
    '''def _recent_refusals() -> int:
    cutoff = time.time() - RECENT_DECISION_WINDOW_SECONDS
    records = common.read_jsonl(common.AUDIT_LOG)
    return sum(1 for record in records if _is_recent_refusal(record, cutoff))


def _is_recent_refusal(record: dict, cutoff: float) -> bool:
    if record.get("decision") != "refuse":
        return False
    raw = record.get("at")
    if not isinstance(raw, str):
        return False
    return _epoch(raw) >= cutoff
''',
    '''def _recent_refusals() -> int:
    return _recent_decisions("refuse")


def _recent_yields() -> int:
    """Turns the end gate permitted while the obligation still stood.

    A blocked action is the evidence that the control worked; a bypass nobody
    reports is the evidence that it did not. This is the second number.
    """
    return _recent_decisions("yield")


def _recent_decisions(decision: str) -> int:
    cutoff = time.time() - RECENT_DECISION_WINDOW_SECONDS
    records = common.read_jsonl(common.AUDIT_LOG)
    return sum(1 for record in records if _is_recent(record, decision, cutoff))


def _is_recent(record: dict, decision: str, cutoff: float) -> bool:
    if record.get("decision") != decision:
        return False
    raw = record.get("at")
    if not isinstance(raw, str):
        return False
    return _epoch(raw) >= cutoff
''',
)

_STATUS_LINE = (
    '''        f"  refusals (24h)     : {_recent_refusals()}\\n"
''',
    '''        f"  refusals (24h)     : {_recent_refusals()}\\n"
        f"  yielded turns (24h): {_recent_yields()}\\n"
''',
)

_STATUS_LIMITS = (
    '''        "the turn-end gate is overridden after 8 consecutive blocks; "
''',
    '''        "the turn-end gate repeats its refusal while the obligation stands "
        "and then yields on the bound, recording the yield; "
''',
)

EDITS: dict[Path, tuple[tuple[str, str], ...]] = {
    CHECKERS: (_NEW_MODULE, _CREATING_TARGETS, _RESEARCH, _RESEARCH_MESSAGE),
    GATE_STOP: (_STOP_DOCSTRING, _STOP_MARKER, _STOP_MAIN),
    STATUS: (_STATUS_COUNTS, _STATUS_LINE, _STATUS_LIMITS),
}

# What the PreToolUse gate must still decide once this is applied.
EXPECTED = (
    ("true", 0, "a command that touches nothing"),
    (f"grep -c x {POLICY}", 0, "reading the policy with grep"),
    (f"echo x >> {POLICY}", 2, "appending to the policy"),
)


def _fail(message: str) -> None:
    sys.stderr.write(f"не применено: {message}\n")


def _backup(stamp: str) -> dict[Path, Path]:
    BACKUPS.mkdir(parents=True, exist_ok=True)
    saved: dict[Path, Path] = {}
    for path in TOUCHED:
        copy = BACKUPS / f"{path.name}.{stamp}"
        shutil.copy2(path, copy)
        saved[path] = copy
    return saved


def _restore(saved: dict[Path, Path]) -> None:
    for path, copy in saved.items():
        shutil.copy2(copy, path)


def _applied_edit(text: str, before: str, after: str) -> str | None:
    if text.count(before) != 1:
        _fail(f"fragment appears {text.count(before)} times: {before[:70]!r}")
        return None
    return text.replace(before, after, 1)


def _patched(path: Path, text: str) -> str | None:
    """One file's patched source, or None when it will not fit."""
    if MARKERS[path] in text:
        _fail(f"{path.name} already carries the change")
        return None
    current: str | None = text
    for before, after in EDITS[path]:
        current = _applied_edit(current, before, after)  # type: ignore[arg-type]
        if current is None:
            return None
    return current


def _gate_verdict(command: str) -> int | None:
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
    for command, expected, description in EXPECTED:
        actual = _gate_verdict(command)
        if actual is None:
            return False
        if actual != expected:
            _fail(f"{description}: expected exit {expected}, gate gave {actual}")
            return False
    return True


def _status_renders() -> bool:
    """The session-start line still renders, and now reports yields."""
    finished = subprocess.run(
        [str(PYTHON), str(STATUS)], capture_output=True, text=True, timeout=120
    )
    if finished.returncode != 0 or "yielded turns" not in finished.stdout:
        _fail(f"status did not render the new line: {finished.stderr[:400]}")
        return False
    return True


def _parses() -> bool:
    try:
        for path in TOUCHED:
            ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as error:
        _fail(f"patched file does not parse: {error}")
        return False
    return True


def _prepared() -> dict[Path, str] | None:
    """Every patched file, or None when any of them cannot be built."""
    for path in (*TOUCHED, POLICY, GATE, PYTHON):
        if not path.exists():
            _fail(f"missing {path}")
            return None
    built: dict[Path, str] = {}
    for path in TOUCHED:
        patched = _patched(path, path.read_text(encoding="utf-8"))
        if patched is None:
            return None
        built[path] = patched
    return built


def _applied(prepared: dict[Path, str]) -> bool:
    """Write, then prove. False on any failure, and the caller restores."""
    try:
        for path, text in prepared.items():
            path.write_text(text, encoding="utf-8")
        return _parses() and _decides_correctly() and _status_renders()
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
    print(f"применено в {len(saved)} файла. копии в {BACKUPS}")
    print("сторож конца хода теперь повторяет отказ и записывает уступку")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
