from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


def _doctor_report(status: str = "ok") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-08-11T00:00:00+00:00",
        "overall_status": status,
        "repaired": [],
        "checks": [],
        "counts": {"ok": 0, "degraded": 0, "error": 0},
        "run_deletion": {
            "schema_version": "run-deletion-snapshot/v1",
            "quiescent": False,
            "permit": False,
            "offline_action_required": True,
            "blockers": [{"code": "legacy_protocol_unquiesced"}],
        },
    }


def test_run_smoke_uses_one_deadline_and_retains_degraded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import install_smoke

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    state_root.mkdir()
    times = iter((100.0, 102.0, 105.0, 106.0))
    observed = []
    monkeypatch.setattr(install_smoke.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        install_smoke,
        "_production_imports",
        lambda: {"mcp": True, "mcp_server": True},
    )

    def doctor(_root: Path, _state_root: Path, timeout: float):
        observed.append(("doctor", timeout))
        return _doctor_report("degraded")

    def mcp(_root: Path, _state_root: Path, timeout: float):
        observed.append(("mcp", timeout))
        return install_smoke.EXPECTED_TOOL_NAMES

    monkeypatch.setattr(install_smoke, "_doctor_report", doctor)
    monkeypatch.setattr(install_smoke, "_mcp_tool_names", mcp)

    report = install_smoke.run_smoke(root, state_root, deadline_seconds=10.0)

    assert report["status"] == "degraded"
    assert report["doctor"] == _doctor_report("degraded")
    assert report["tool_count"] == 12
    assert observed == [("doctor", 8.0), ("mcp", 5.0)]


@pytest.mark.parametrize(
    ("returncode", "stdout", "message"),
    [
        (2, json.dumps(_doctor_report("error")), "Doctor failed"),
        (0, "not-json", "valid JSON"),
        (0, json.dumps({"overall_status": "ok"}), "schema"),
    ],
)
def test_doctor_rejects_error_exit_malformed_json_and_invalid_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    message: str,
) -> None:
    import install_smoke

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    state_root.mkdir()
    monkeypatch.setattr(
        install_smoke,
        "_run_process_tree",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode, stdout, ""),
    )

    with pytest.raises(RuntimeError, match=message):
        install_smoke._doctor_report(root, state_root, 5.0)


def test_doctor_accepts_returncode_one_only_for_degraded_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import install_smoke

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    (root / "scripts").mkdir(parents=True)
    state_root.mkdir()
    expected = _doctor_report("degraded")
    monkeypatch.setattr(
        install_smoke,
        "_run_process_tree",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, json.dumps(expected), "warning"
        ),
    )

    assert install_smoke._doctor_report(root, state_root, 5.0) == expected


def test_real_stdio_mcp_initializes_and_lists_exact_tool_surface(
    tmp_path: Path,
) -> None:
    import install_smoke

    root = Path(__file__).resolve().parent.parent
    names = install_smoke._mcp_tool_names(root, tmp_path, 20.0)

    assert set(names) == set(install_smoke.EXPECTED_TOOL_NAMES)
    assert len(names) == len(install_smoke.EXPECTED_TOOL_NAMES)


def test_tool_contract_rejects_same_size_surface_with_wrong_name() -> None:
    import install_smoke

    unexpected = (*install_smoke.EXPECTED_TOOL_NAMES[:-1], "unexpected")

    with pytest.raises(RuntimeError, match="unexpected tool contract"):
        install_smoke.validate_tool_contract(unexpected)


def test_production_import_probe_has_no_pytest_dependency() -> None:
    import install_smoke

    imports = install_smoke._production_imports()
    source = Path(install_smoke.__file__).read_text(encoding="utf-8")

    assert imports["mcp"] is True
    assert imports["mcp_server"] is True
    assert "import pytest" not in source
    if sys.version_info < (3, 11):
        assert imports["tomli"] is True


def test_cli_emits_one_json_object_or_bounded_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import install_smoke

    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    root.mkdir()
    state_root.mkdir()
    expected = {"status": "ok", "tool_count": 12}
    monkeypatch.setattr(install_smoke, "run_smoke", lambda *args, **kwargs: expected)

    assert install_smoke.main(
        ["--root", str(root), "--state-root", str(state_root)]
    ) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == expected
    assert output.err == ""

    monkeypatch.setattr(
        install_smoke,
        "run_smoke",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret detail")),
    )
    assert install_smoke.main(
        ["--root", str(root), "--state-root", str(state_root)]
    ) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert len(output.err.encode("utf-8")) <= install_smoke.MAX_ERROR_BYTES
    assert "secret detail" not in output.err
