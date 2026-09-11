"""Which Codex hook commands belong to llm-wiki: one definition (#24, C2).

The installer's ownership merge (`codex_memory`) and the doctor's runtime
verification (`doctor`) ask the same question of a Codex hook command; two
answers would let the installer write a hook the doctor then calls foreign.
A command is ours when it names one of our scripts and ends with that
script's argument tail: the lifecycle hook, and the graph hint and reminder.
"""

from __future__ import annotations

OUR_CODEX_COMMANDS = (("codex_memory.py", " hook"), ("graph_hint.py", " --source codex"))
OUR_CODEX_SCRIPTS = tuple(script for script, _ending in OUR_CODEX_COMMANDS)


def is_our_codex_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    tail = command.rstrip()
    return any(script in tail and tail.endswith(ending) for script, ending in OUR_CODEX_COMMANDS)
