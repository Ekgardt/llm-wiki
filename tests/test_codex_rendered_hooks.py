"""Rendered commands keep Codex's runtime trust verdict authoritative.

The template carries one command per platform (`command`, `commandWindows`),
and the doctor expects the one for the platform it runs on. The rendered
form — `env … ~/.local/bin/uv …` — is what the POSIX installer writes, and
the doctor recognises it on POSIX only; those tests are POSIX tests.
"""
import json
import os
from pathlib import Path

import doctor
import pytest

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ('uv run --locked --no-sync --directory "$LLM_WIKI_ROOT" '
           'python "$LLM_WIKI_ROOT/scripts/codex_memory.py" hook')
posix_rendering = pytest.mark.skipif(
    os.name == "nt", reason="the rendered command is the POSIX installation form"
)


def _installed_command(root: Path) -> str:
    return (f"env LLM_WIKI_ROOT={root} LLM_WIKI_STATE_ROOT={root} "
            f"{Path.home()}/.local/bin/uv run --locked --no-sync --directory {root} "
            f"python {root}/scripts/codex_memory.py hook")


def _template(root: Path) -> Path:
    template = json.loads((ROOT / "integrations/codex/hooks.json").read_text())
    destination = root / "integrations/codex/hooks.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(template))
    return destination


def _template_command(root: Path) -> str:
    """The template's own command for this platform."""
    return doctor._expected_codex_runtime_hooks(_template(root))[0]["command"]


def _verdict(root: Path, command: str, trust: str = "trusted"):
    destination = _template(root)
    hooks = [dict(wanted, eventName=wanted["eventName"][0].lower() + wanted["eventName"][1:],
                  command=command, enabled=True, trustStatus=trust)
             for wanted in doctor._expected_codex_runtime_hooks(destination)]
    return doctor._codex_hooks_verdict(root, hooks)


@pytest.mark.parametrize("trust,expected", [
    ("trusted", (True, "runtime_hooks_active")),
    ("untrusted", (False, "runtime_hooks_untrusted")),
    ("modified", (False, "runtime_hooks_modified")),
])
def test_template_command_preserves_native_trust_verdict(tmp_path, trust, expected):
    assert _verdict(tmp_path, _template_command(tmp_path), trust) == expected


@posix_rendering
@pytest.mark.parametrize("trust,expected", [
    ("trusted", (True, "runtime_hooks_active")),
    ("untrusted", (False, "runtime_hooks_untrusted")),
    ("modified", (False, "runtime_hooks_modified")),
])
def test_rendered_command_preserves_native_trust_verdict(tmp_path, trust, expected):
    assert _verdict(tmp_path, _installed_command(tmp_path), trust) == expected


@pytest.mark.parametrize("old,new", [
    ("LLM_WIKI_STATE_ROOT=", "OTHER_ENV="),
    ("--locked", "--offline"),
    ("--no-sync", "--no-sync --extra malicious"),
    ("python ", "python -c "),
    ("codex_memory.py", "other.py"),
    ("/.local/bin/uv", "/foreign/bin/uv"),
    ("env ", "env EXTRA=1 "),
])
@posix_rendering
def test_foreign_or_changed_rendering_is_refused(tmp_path, old, new):
    command = _installed_command(tmp_path).replace(old, new)
    assert _verdict(tmp_path, command) == (False, "runtime_hooks_mismatch")


@posix_rendering
def test_foreign_root_and_shell_suffix_are_refused(tmp_path):
    assert _verdict(tmp_path, _installed_command(tmp_path / "foreign")) == (
        False, "runtime_hooks_mismatch")
    assert _verdict(tmp_path, _installed_command(tmp_path) + "; echo hook") == (
        False, "runtime_hooks_mismatch")


def test_original_template_remains_supported(tmp_path):
    assert _verdict(tmp_path, _template_command(tmp_path)) == (True, "runtime_hooks_active")


@posix_rendering
def test_the_posix_template_is_the_posix_command(tmp_path):
    assert _template_command(tmp_path) == COMMAND


def test_a_hook_still_requires_enabled_and_unique_match(tmp_path):
    command = _template_command(tmp_path)
    wanted = dict(eventName="Stop", matcher=None, command=command)
    hook = dict(wanted, command=command, enabled=False, trustStatus="trusted")
    assert doctor._codex_hook_problem(wanted, [hook], tmp_path) == "runtime_hooks_disabled"
    assert doctor._codex_hook_problem(wanted, [hook, hook], tmp_path) == "runtime_hooks_mismatch"


def test_rendered_hook_does_not_accept_wrong_matcher(tmp_path):
    wanted = dict(eventName="Stop", matcher=None, command=COMMAND)
    hook = dict(wanted, command=_installed_command(tmp_path), matcher="other")
    assert doctor._matching_hooks(wanted, [hook], tmp_path) == []


def test_event_aliases_are_exact_not_case_insensitive(tmp_path):
    wanted = dict(eventName="Stop", matcher=None, command=COMMAND)
    hook = dict(wanted, command=_installed_command(tmp_path), eventName="STOP")
    assert doctor._matching_hooks(wanted, [hook], tmp_path) == []
