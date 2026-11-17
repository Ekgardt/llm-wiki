"""The source text of a named function in a shell script, for running it on its own."""
from __future__ import annotations

import re


def _shell_function(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^{re.escape(name)}\(\) \{{.*?^\}}", source)
    assert match, f"{name} missing"
    return match.group(0)


def _shell_functions(source: str, *names: str) -> str:
    return "\n".join(_shell_function(source, name) for name in names)
