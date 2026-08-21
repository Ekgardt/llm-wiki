"""The enforcement patch still fits the files it was written against.

`docs/enforcement/apply-rules-1-2-cover-bash.py` refuses to run unless the gate
sources are exactly what it expects. That refusal is the safety property, and
it is worth knowing it holds before someone runs the script as root rather than
after. On a machine without the gate installed there is nothing to check and
these skip.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docs" / "enforcement" / "apply-rules-1-2-cover-bash.py"
ENFORCEMENT = Path("/etc/claude-code/enforcement")
CHECKERS = ENFORCEMENT / "checkers.py"
POLICY = ENFORCEMENT / "rules-policy.json"


def _module():
    """The apply script, loaded as a module so its steps can be exercised."""
    spec = importlib.util.spec_from_file_location("apply_enforcement_patch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _applies_to(document: dict, rule_id: str) -> list:
    """What one policy entry applies to."""
    for entry in document["entries"]:
        if entry["id"] == rule_id:
            return entry["applies_to"]
    raise KeyError(rule_id)


@pytest.fixture(scope="module")
def patcher():
    if not SCRIPT.is_file():
        pytest.skip("the apply script is not present")
    return _module()


@pytest.fixture(scope="module")
def installed_gate():
    if not CHECKERS.is_file() or not POLICY.is_file():
        pytest.skip("the enforcement gate is not installed on this machine")
    return CHECKERS.read_text(encoding="utf-8"), POLICY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def unpatched_gate(installed_gate):
    """The gate before the change. Skips once the change is in place."""
    checkers_text, policy_text = installed_gate
    if "_bash_write_targets" in checkers_text:
        pytest.skip("the change is already applied")
    return checkers_text, policy_text


def test_the_anchor_still_matches_the_installed_checkers(patcher, unpatched_gate):
    """A patch written against a different version must refuse, not half-apply."""
    checkers_text, _policy_text = unpatched_gate

    patched = patcher._patched_checkers(checkers_text)

    assert patched is not None, "the anchor no longer matches checkers.py"
    ast.parse(patched)


def test_the_patched_checkers_carry_the_resolver(patcher, unpatched_gate):
    """Widening the policy alone changes nothing without a target for Bash."""
    checkers_text, _policy_text = unpatched_gate

    patched = patcher._patched_checkers(checkers_text)

    assert "_bash_write_targets" in (patched or "")


def test_applying_twice_is_refused(patcher, installed_gate):
    """Running it again must not stack a second copy of the resolver.

    Holds from either side: against the gate as it stands once the change is
    applied, and against the patched text before it is.
    """
    checkers_text, _policy_text = installed_gate
    once = patcher._patched_checkers(checkers_text) or checkers_text

    assert patcher._patched_checkers(once) is None


def test_the_three_rules_take_bash(patcher, installed_gate):
    """Rules 1 and 2 are the ones a shell heredoc walked around."""
    _checkers_text, policy_text = installed_gate
    document = json.loads(policy_text)

    assert patcher._patched_policy(document) is True

    missing = [
        rule_id
        for rule_id in patcher.RULE_IDS
        if "Bash" not in _applies_to(document, rule_id)
    ]
    assert missing == []


def test_rule_five_is_left_alone(patcher, installed_gate):
    """Rule 5 is enforced from user scope; its entry must not be touched."""
    _checkers_text, policy_text = installed_gate
    document = json.loads(policy_text)
    patcher._patched_policy(document)

    assert "Bash" not in _applies_to(document, "R5-complexity")


def test_a_policy_missing_an_entry_is_refused(patcher):
    """Silently skipping a rule would leave a gap nobody is told about."""
    assert patcher._patched_policy({"entries": []}) is False
