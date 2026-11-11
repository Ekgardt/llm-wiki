"""Installed command values and recognition, without configuration or install IO."""
from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

OUR_CODEX_COMMANDS = (
    ("codex_memory.py", " hook"),
    ("graph_hint.py", " --source codex"),
    ("integration_adapter.py", " --source codex --event user_prompt"),
    ("integration_adapter.py", " --source codex --event post_tool_use"),
)

OUR_CODEX_SCRIPTS = tuple(dict.fromkeys(script for script, _ending in OUR_CODEX_COMMANDS))

PROVIDER_ENV_KEYS = (
    "MEMORY_LLM_PROVIDER",
    "MEMORY_CLAUDE_MODEL",
    "MEMORY_CODEX_MODEL",
    "MEMORY_CODEX_REASONING",
    "MEMORY_LLM_MODEL",
    "MEMORY_LLM_BASE_URL",
    "OLLAMA_NO_CLOUD",
)

UNPERSISTED_PROVIDERS = frozenset({"fake"})

CODEX_BOOTSTRAP_BODY = '''import base64
import builtins
import json
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

KEYS={keys!r}
SCRIPTS={scripts!r}
def require_values(value):
    if type(value) is not dict or not set(value).issubset(KEYS):
        raise ValueError("invalid installed provider keys")
    if any(type(item) is not str for item in value.values()):
        raise ValueError("invalid installed provider values")
    return value
def require_payload(encoded):
    value=require_values(json.loads(base64.b64decode(encoded,validate=True)))
    canonical=base64.b64encode(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).decode()
    if encoded != canonical:
        raise ValueError("noncanonical installed provider payload")
    if value.get("MEMORY_LLM_PROVIDER","").strip().lower() == "fake":
        raise ValueError("test provider cannot be installed")
    return value
def target_path(argument):
    target=Path(argument)
    absolute=Path(os.path.abspath(target))
    if absolute.resolve(strict=True) != absolute or not absolute.is_file():
        raise ValueError("invalid installed provider target")
    if absolute.parent.name != "scripts" or absolute.name not in SCRIPTS:
        raise ValueError("invalid installed provider script")
    return absolute
def script_module(target):
    module=ModuleType("__main__")
    module.__dict__.update(__file__=str(target),__package__=None,__spec__=None,
        __cached__=None,__loader__=SourceFileLoader("__main__",str(target)),__builtins__=builtins)
    return module
def prepare_import_path(target):
    if getattr(sys.flags,"safe_path",False) or sys.flags.isolated:
        return
    sys.path[0]=str(target.parent)
def launch():
    bundle=require_payload(sys.argv[1])
    target=target_path(sys.argv[2])
    source=target.read_bytes()
    if set(KEYS).isdisjoint(os.environ):
        os.environ.update(bundle)
    sys.argv=sys.argv[2:]
    prepare_import_path(target)
    module=script_module(target)
    sys.modules["__main__"]=module
    exec(compile(source,str(target),"exec",dont_inherit=True),module.__dict__)
launch()
'''

def codex_provider_bootstrap() -> str:
    import base64

    program = CODEX_BOOTSTRAP_BODY.format(keys=PROVIDER_ENV_KEYS, scripts=(*OUR_CODEX_SCRIPTS, "mcp_server.py"))
    encoded = base64.b64encode(program.encode("utf-8")).decode("ascii")
    return f"exec(__import__('base64').b64decode('{encoded}'))"

def require_codex_provider_bundle(bundle: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(bundle, Mapping) or not set(bundle).issubset(PROVIDER_ENV_KEYS):
        raise ValueError("invalid installed provider keys")
    if any(type(value) is not str for value in bundle.values()):
        raise ValueError("invalid installed provider values")
    return require_persistable_provider(dict(bundle))

def require_persistable_provider(bundle: dict[str, str]) -> dict[str, str]:
    if bundle.get("MEMORY_LLM_PROVIDER", "").strip().lower() in UNPERSISTED_PROVIDERS:
        raise ValueError("test provider cannot be installed")
    return bundle

def codex_provider_payload(bundle: Mapping[str, str]) -> str:
    import base64

    value = require_codex_provider_bundle(bundle)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return base64.b64encode(encoded.encode("utf-8")).decode("ascii")

def codex_provider_from_payload(payload: str) -> dict[str, str]:
    import base64

    value = require_codex_provider_bundle(json.loads(base64.b64decode(payload, validate=True)))
    if codex_provider_payload(value) != payload:
        raise ValueError("noncanonical installed provider payload")
    return value

def codex_launch_bundle(args: Sequence[object], root: Path) -> dict[str, str]:
    if len(args) < 10 or list(args[:7]) != ["run", "--locked", "--no-sync", "--directory",
                                        str(root.resolve()), "python", "-c"]:
        raise ValueError("invalid installed Codex arguments")
    if args[7] != codex_provider_bootstrap() or type(args[8]) is not str:
        raise ValueError("invalid installed Codex bootstrap")
    return codex_target_bundle(args, root)

def codex_target_bundle(args: Sequence[object], root: Path) -> dict[str, str]:
    target = Path(str(args[9]))
    target = target if target.is_absolute() else root.resolve() / target
    if target.parent != root.resolve() / "scripts" or target.name not in (*OUR_CODEX_SCRIPTS, "mcp_server.py"):
        raise ValueError("invalid installed Codex target")
    return codex_provider_from_payload(str(args[8]))

def codex_windows_argument(argument: str) -> str:
    import subprocess

    rendered = subprocess.list2cmdline([argument])
    if rendered.startswith('"'):
        return rendered
    trailing = len(argument) - len(argument.rstrip('\\'))
    return '"' + rendered + '\\' * trailing + '"'

def codex_windows_command(arguments: Sequence[str]) -> str:
    return arguments[0] + " " + " ".join(codex_windows_argument(item) for item in arguments[1:])

def codex_command_words(command: str, windows: bool) -> list[str]:
    import shlex

    if windows:
        return codex_windows_words(command)
    return shlex.split(command)

def codex_windows_words(command: str) -> list[str]:
    import shlex

    if os.name == "nt":
        import win32api

        return win32api.CommandLineToArgv(command)
    words = shlex.split(command, posix=False)
    return [codex_windows_word(word) for word in words]

def codex_windows_word(word: str) -> str:
    if word.startswith('"') and word.endswith('"'):
        return word[1:-1]
    return word

def codex_parsed_launch(command: str, windows: bool) -> tuple[Path, dict[str, str]]:
    import shlex

    words = codex_command_words(command, windows)
    root = Path(words[words.index("--directory") + 1])
    bundle = codex_launch_bundle(words[1:], root)
    require_codex_hook_tail(words[1:])
    canonical = codex_windows_command(words) if windows else shlex.join(words)
    if words[0] != "uv" or canonical != command:
        raise ValueError("noncanonical installed Codex command")
    return root, bundle

def require_codex_hook_tail(args: Sequence[str]) -> None:
    target = Path(args[9]).name
    tail = " " + " ".join(args[10:])
    if (target, tail) not in OUR_CODEX_COMMANDS:
        raise ValueError("invalid installed Codex hook tail")

def codex_command_launch(command: str) -> tuple[Path, dict[str, str]]:
    for windows in (False, True):
        try:
            return codex_parsed_launch(command, windows)
        except (ValueError, TypeError, IndexError, UnicodeError):
            continue
    raise ValueError("invalid installed Codex command")

# Preserve existing serialization/import paths while consumers migrate.
codex_command_words.__module__ = "integration_hook_config"
codex_parsed_launch.__module__ = "integration_hook_config"
codex_target_bundle.__module__ = "integration_hook_config"
codex_windows_argument.__module__ = "integration_hook_config"
codex_windows_command.__module__ = "integration_hook_config"
codex_windows_word.__module__ = "integration_hook_config"
codex_windows_words.__module__ = "integration_hook_config"
require_codex_hook_tail.__module__ = "integration_hook_config"
require_codex_provider_bundle.__module__ = "integration_hook_config"
require_persistable_provider.__module__ = "integration_hook_config"
codex_command_launch.__module__ = "integration_hook_config"
codex_launch_bundle.__module__ = "integration_hook_config"
codex_provider_bootstrap.__module__ = "integration_hook_config"
codex_provider_from_payload.__module__ = "integration_hook_config"
codex_provider_payload.__module__ = "integration_hook_config"
