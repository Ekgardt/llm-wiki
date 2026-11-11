"""Which Codex hook commands belong to llm-wiki: one definition (#24, C2).

The installer's ownership merge (`codex_memory`) and the doctor's runtime
verification (`doctor`) ask the same question of a Codex hook command; two
answers would let the installer write a hook the doctor then calls foreign.
A command is ours when it names one of our scripts and ends with that
script's argument tail: the lifecycle hook, the graph hint and reminder, and
the prompt and edit capture (docs/research/2026-09-11-codex-leaves-breadcrumbs-too.md).
"""

from __future__ import annotations

from llm_wiki.integration_launch_values import (
    OUR_CODEX_COMMANDS as OUR_CODEX_COMMANDS,
)
from llm_wiki.integration_launch_values import (
    OUR_CODEX_SCRIPTS as OUR_CODEX_SCRIPTS,
)


def is_our_codex_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    if "exec(__import__" in command:
        return _owned_bootstrap_command(command)
    tail = command.rstrip()
    return any(script in tail and tail.endswith(ending) for script, ending in OUR_CODEX_COMMANDS)


def _owned_bootstrap_command(command: str) -> bool:
    from llm_wiki.integration_launch_values import codex_command_launch

    try:
        codex_command_launch(command)
        return True
    except (ValueError, TypeError, UnicodeError):
        return False


def is_llm_wiki_hook(handler: object) -> bool:
    """Ours when either platform's command is one of ours (`codex_hook_identity`)."""
    from codex_hook_identity import is_our_codex_command

    if not isinstance(handler, dict):
        return False
    commands = (handler.get("command"), handler.get("commandWindows"))
    return any(is_our_codex_command(command) for command in commands)
