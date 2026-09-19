"""A refused publication tells the operator which file stands in the way.

The name is the file's place inside the image, never a path on this machine.
See docs/research/2026-09-17-a-refused-publication-names-its-file.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_private_vault_backup import _staged_image


def _populated_target(tmp_path: Path) -> Path:
    target = tmp_path / "new-machine-secret-name"
    (target / "knowledge/notes").mkdir(parents=True)
    (target / "run").mkdir(parents=True)
    (target / "knowledge/notes/private.md").write_bytes(b"someone else's work\n")
    return target


def test_the_command_line_names_the_conflicting_file_by_its_image_path(
    tmp_path, monkeypatch, capsys
):
    import private_vault_backup as backup

    image, digest = _staged_image(tmp_path)
    target = _populated_target(tmp_path)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(target))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(target))

    result = backup.main(["publish", "--image", str(image), "--manifest-sha256", digest])

    output = capsys.readouterr().out
    assert result == 2
    assert json.loads(output) == {
        "ok": False,
        "code": "publish_conflict",
        "details": ["vault/knowledge/notes/private.md"],
    }
    assert str(tmp_path) not in output


def test_a_machine_path_or_a_climbing_path_is_never_printed():
    import private_vault_backup as backup

    details = ("/srv/vault/knowledge/x.md", "vault/../etc/passwd", "vault/", "state/run/a b.md")

    assert backup._safe_error_details(details) == ["state/run/a b.md"]
