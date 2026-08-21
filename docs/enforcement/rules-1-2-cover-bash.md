# Rules 1 and 2 do not cover shell-made edits

Status: proposed. The change is in root-owned enforcement files, which rule 4
reserves for the machine owner's deliberate act, so it is written down here
rather than applied.

## What happened

On 2026-08-21 a session made several hundred code changes in `llm-wiki` and
rules 1, 2 and 5 did not fire once. Nothing was switched off and no check was
faulty. Every edit simply arrived as a shell heredoc:

    python3 - <<'PY'
    from pathlib import Path
    p = Path("scripts/compile_memory.py")
    p.write_text(...)
    PY

## Why the gates let it through

The policy scopes those rules to the tools that edit files:

    "id": "R1-graph-consulted",       "applies_to": ["Edit", "Write", "NotebookEdit"]
    "id": "R1-graph-fresh",           "applies_to": ["Edit", "Write", "NotebookEdit"]
    "id": "R2-research-before-architecture", "applies_to": ["Edit", "Write", "NotebookEdit"]

A heredoc is a `Bash` call, so none of the three applied. The complexity gate
had the same gap from the other side: its `PostToolUse` matcher in
`managed-settings.json` is `Edit|Write|NotebookEdit`.

Both checkers also start from `tool_input.file_path`, which a `Bash` payload
does not carry, so widening `applies_to` alone would change nothing —
`_target_path` returns `None` and both rules pass by default.

## The change

Two parts, both small.

`checkers.py` — resolve a target from a shell command that can write. Only the
rule 1 and rule 2 checkers use `_target_path`, so nothing else moves.

```python
def _target_path(payload: dict) -> Path | None:
    raw = (payload.get("tool_input") or {}).get("file_path")
    if isinstance(raw, str) and raw:
        return Path(raw)
    targets = _bash_write_targets(payload)
    return targets[0] if targets else None


# Forms that can leave a file different from how they found it. A gate that
# only sees Edit and Write is a gate a shell heredoc walks around.
_BASH_WRITE_FORMS = (
    r">>?\s*(?!/dev/)\S",
    r"(?<!\w)tee(?!\w)",
    r"(?<!\w)(?:sed|perl)\s+[^\n;|]*(?<![\w-])-i(?![\w-])",
    r"(?<!\w)(?:python3?|node|ruby|perl|deno|bun)\s+[^\n;|]*(?:(?<![\w-])-c(?![\w-])|<<)",
    r"(?<!\w)(?:patch|git\s+apply)(?!\w)",
    r"(?<!\w)(?:mv|cp|install|truncate|dd)(?!\w)",
)

_PATH_IN_COMMAND = re.compile(r"[\w./-]*[\w-]+\.[A-Za-z0-9]{1,8}")


def _bash_write_targets(payload: dict) -> list[Path]:
    """Repository files a shell command names, when the command can write.

    Undecidable in general: the path can be built at run time inside the
    heredoc. This catches the literal spelling, which is how a shell edit is
    nearly always written. A floor, not a proof — stated as a limit for the
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
```

`rules-policy.json` — add `"Bash"` to `applies_to` for `R1-graph-consulted`,
`R1-graph-fresh` and `R2-research-before-architecture`.

## What it costs

A shell command that merely reads but happens to name a repository file — an
analysis heredoc, say — now needs a graph query inside the same window. One
`search_graph` covers ninety minutes, so the cost is one call per working
session, and the alternative is the gap that produced this document.

## What is already closed

Rule 5 needed no change in `/etc`. `~/.claude/tools/ccn_gate_bash.py` runs the
same complexity gate over repository source files a `Bash` call left newer than
its own start, registered as a `PostToolUse` hook on `Bash` in
`~/.claude/settings.json`. Checked both ways: a nested function written through
a heredoc is refused with exit 2, and clean code, non-Bash tools and paths
outside a repository pass.

Its limit is a window rather than a proof — the hook is not told when the
command began — so a file left untouched for two minutes before an unrelated
shell call is gated again. It re-runs a check that already passed; it does not
miss one.
