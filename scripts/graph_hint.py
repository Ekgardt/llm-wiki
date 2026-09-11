"""Hook adapter: graph context where the agent searches (#24, C1 and C2).

One entry for three hosts, each a thin adapter over the same two answers:

* a **hint** for a search whose pattern looks like a code symbol - Claude Code
  `PreToolUse` of `Grep`/`Glob`, Codex `PostToolUse` of `Bash` running
  `rg`/`grep`, the OpenCode plugin after its `grep`/`glob` tool;
* a **reminder** naming the code tools, for Claude Code and Codex
  `SubagentStart` (a session start gets the same line from the adapter).

Both are read from the checkout's hint table (`code_hints.py`), never from the
generation reader, so an answer costs a Git identity probe and one indexed
SQLite read. The adapter never blocks a tool call and never fails one: any
error, an unindexed checkout, a pattern that is not an identifier or a name
the graph does not hold all answer nothing, with exit status 0. Research:
`docs/research/2026-09-11-the-graph-meets-the-agent-where-it-searches.md`.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

MAX_INPUT_BYTES = 64 * 1024
SCOPE_DEADLINE_SECONDS = 2.0
SOURCES = ("claude", "codex", "opencode")
SEARCH_COMMANDS = frozenset({"rg", "grep", "egrep", "fgrep", "ag", "ack", "git-grep"})
SHELL_OPERATORS = frozenset({"|", "||", "&&", ";", "&", ">", ">>", "<"})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,127}(?:\.[A-Za-z_][A-Za-z0-9_]{0,127}){0,3}")
_DEFINITION_PREFIX = re.compile(r"^(?:def|class|function|func|fn|async def)\s+")
_CALL_SUFFIX = re.compile(r"(?:\\?\()+$")
_GLOB_CHARACTERS = re.compile(r"[*?\[\]{}]")


# --------------------------------------------------------------------------
# what was searched for
# --------------------------------------------------------------------------


def symbol_candidate(pattern: object) -> str | None:
    """The identifier a search pattern names, or None for anything else."""
    if not isinstance(pattern, str):
        return None
    text = _DEFINITION_PREFIX.sub("", pattern.strip().replace("\\b", ""))
    text = _CALL_SUFFIX.sub("", text.strip("^$ "))
    return text if _IDENTIFIER.fullmatch(text) else None


def glob_candidate(pattern: object) -> str | None:
    """A file glob names a symbol only through its stem: `**/*load_corpus*`."""
    if not isinstance(pattern, str):
        return None
    stem = os.path.splitext(_GLOB_CHARACTERS.sub("", pattern.rsplit("/", 1)[-1]))[0]
    return symbol_candidate(stem)


def _command_tokens(command: object) -> list[str]:
    if not isinstance(command, str):
        return []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    cut = next((index for index, token in enumerate(tokens) if token in SHELL_OPERATORS), len(tokens))
    return tokens[:cut]


def _is_search_command(token: str) -> bool:
    """`git grep` reaches here as its `grep` token, which is itself a search."""
    return Path(token).name in SEARCH_COMMANDS


def _search_start(tokens: list[str]) -> int | None:
    return next((index for index, token in enumerate(tokens) if _is_search_command(token)), None)


def _positional(tokens: list[str]) -> list[str]:
    return [token for token in tokens if not token.startswith("-")]


def _search_arguments(tokens: list[str]) -> list[str]:
    start = _search_start(tokens)
    if start is None:
        return []
    return _positional(tokens[start + 1 :])


def command_candidate(command: object) -> str | None:
    """The first identifier argument of a shell search, `rg -n load_corpus scripts/`."""
    arguments = _search_arguments(_command_tokens(command))
    return next((found for found in map(symbol_candidate, arguments) if found), None)


# --------------------------------------------------------------------------
# where it was searched
# --------------------------------------------------------------------------


def _directory(payload: dict, tool_input: dict) -> Path | None:
    """The searched path when it is an absolute directory, else the session's."""
    for value in (tool_input.get("path"), payload.get("cwd"), payload.get("directory")):
        if isinstance(value, str) and os.path.isabs(value) and os.path.isdir(value):
            return Path(value)
    return None


def _scope(directory: Path | None):
    if directory is None:
        return None
    from repository_scope import resolve_repository_scope

    scope = resolve_repository_scope(directory, deadline=time.monotonic() + SCOPE_DEADLINE_SECONDS)
    return scope if scope.git_common_dir is not None else None


def _state_root() -> Path:
    from repository_index import state_root_path

    return state_root_path()


# --------------------------------------------------------------------------
# answers
# --------------------------------------------------------------------------


def hint_for(directory: Path | None, symbol: str | None) -> str | None:
    import code_hints

    scope = _scope(directory) if symbol else None
    if scope is None:
        return None
    answer = code_hints.lookup_symbol(_state_root(), scope.checkout_id, symbol)
    return code_hints.hint_text(answer, scope.git_commit)


def reminder_for(directory: Path | None) -> str | None:
    import code_hints

    scope = _scope(directory)
    if scope is None:
        return None
    return code_hints.reminder_text(code_hints.read_meta(code_hints.hints_path(_state_root(), scope.checkout_id)))


def _tool_input(payload: dict, key: str) -> dict:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _claude_search(payload: dict) -> str | None:
    tool_input = _tool_input(payload, "tool_input")
    readers = {"Grep": symbol_candidate, "Glob": glob_candidate}
    reader = readers.get(payload.get("tool_name"))
    if reader is None:
        return None
    return hint_for(_directory(payload, tool_input), reader(tool_input.get("pattern")))


def _codex_search(payload: dict) -> str | None:
    tool_input = _tool_input(payload, "tool_input")
    if payload.get("tool_name") != "Bash":
        return None
    return hint_for(_directory(payload, tool_input), command_candidate(tool_input.get("command")))


def _subagent_reminder(payload: dict) -> str | None:
    return reminder_for(_directory(payload, {}))


def _opencode_search(payload: dict) -> str | None:
    arguments = _tool_input(payload, "args")
    readers = {"grep": symbol_candidate, "glob": glob_candidate}
    reader = readers.get(str(payload.get("tool", "")).lower())
    if reader is None:
        return None
    return hint_for(_directory(payload, arguments), reader(arguments.get("pattern")))


_HOOK_ANSWERS = {
    ("claude", "PreToolUse"): _claude_search,
    ("claude", "SubagentStart"): _subagent_reminder,
    ("codex", "PostToolUse"): _codex_search,
    ("codex", "SubagentStart"): _subagent_reminder,
}


def _hook_output(event: str, text: str | None) -> dict:
    if not text:
        return {}
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def answer(source: str, payload: dict) -> dict:
    """The host-shaped output for one event; an empty object means silence."""
    if source == "opencode":
        text = _opencode_search(payload)
        return {"context": text} if text else {}
    event = str(payload.get("hook_event_name", ""))
    handler = _HOOK_ANSWERS.get((source, event))
    return _hook_output(event, handler(payload) if handler else None)


def _read_payload() -> dict:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        return {}
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else {}


def _source(argv: list[str]) -> str:
    if len(argv) == 2 and argv[0] == "--source" and argv[1] in SOURCES:
        return argv[1]
    raise ValueError("usage: graph_hint.py --source claude|codex|opencode")


def main(argv: list[str] | None = None) -> int:
    """Never block, never fail the host: every error is silence."""
    try:
        output = answer(_source(list(sys.argv[1:] if argv is None else argv)), _read_payload())
    except Exception:  # noqa: BLE001 - a hint must never break the tool call it rides on
        return 0
    if output:
        sys.stdout.write(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
