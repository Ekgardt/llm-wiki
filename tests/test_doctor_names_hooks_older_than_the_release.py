"""Doctor names installed Claude hooks that still pass a retired delegate.

See docs/research/2026-09-25-doctor-names-hooks-older-than-the-release.md.
"""

from __future__ import annotations

from pathlib import Path

import doctor
import integration_adapter


def _claude_settings(home: Path, command: str) -> Path:
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"env": {"LLM_WIKI_ROOT": "/v"}, "hooks": {"x": "' + command + '"}}', encoding="utf-8")
    return path


def _claude(home: Path) -> dict:
    configs = doctor._integration_host_configs(home)
    return doctor._generic_host_result(*configs["claude"])


def test_hooks_that_name_a_retired_delegate_ask_for_the_installer(tmp_path: Path) -> None:
    _claude_settings(tmp_path, "integration_adapter.py --event pre_compact --delegate precompact_capture.py")

    result = _claude(tmp_path)

    assert (result["status"], "rerun the installer" in result["message"]) == ("degraded", True)


def test_current_hooks_are_ok(tmp_path: Path) -> None:
    _claude_settings(tmp_path, "integration_adapter.py --event pre_compact")

    assert _claude(tmp_path)["status"] == "ok"


def test_doctor_knows_every_retired_name_the_adapter_still_tolerates() -> None:
    assert set(doctor.RETIRED_HOOK_DELEGATES) == set(integration_adapter.CAPTURE_DELEGATES.values())
