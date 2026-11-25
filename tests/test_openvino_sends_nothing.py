"""OpenVINO is told to send nothing before its first import and runs on the given threads.

`import openvino` imports its converter, and the converter sends a usage event
unless the consent file declines; a missing file counts as consent. The memory
writes the declining value first and keeps a choice already on disk
(owner message 17943).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import openvino_runtime
import pytest

from tests.slow_machine import LONG_TIMEOUT

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_a_missing_consent_is_written_as_declined(home: Path) -> None:
    openvino_runtime.decline_telemetry()

    assert openvino_runtime.telemetry_consent_file().read_text(encoding="utf-8").strip() == "0"
    assert openvino_runtime.telemetry_declined()


def test_a_choice_already_recorded_is_kept(home: Path) -> None:
    consent = openvino_runtime.telemetry_consent_file()
    consent.parent.mkdir(parents=True)
    consent.write_text("1\n", encoding="utf-8")

    openvino_runtime.decline_telemetry()

    assert (consent.read_text(encoding="utf-8"), openvino_runtime.telemetry_declined()) == (
        "1\n",
        False,
    )


def test_the_consent_is_declined_before_openvino_is_imported(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    class _Core:
        def compile_model(
            self, path: str, device: str, config: dict[str, object]
        ) -> tuple[str, str, dict[str, object]]:
            return (path, device, config)

    class _Importing:
        def __getattr__(self, name: str) -> type[_Core]:
            seen.append(openvino_runtime.telemetry_declined())
            return _Core

    monkeypatch.setitem(sys.modules, "openvino", _Importing())

    path, device, _config = openvino_runtime.compiled_model(home / "model.onnx")

    assert (set(seen), path, device) == ({True}, str(home / "model.onnx"), "CPU")


def test_a_consent_that_cannot_be_written_stops_the_load(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "intel").write_text("a file where the directory should be", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "openvino", None)

    with pytest.raises(OSError):
        openvino_runtime.compiled_model(home / "model.onnx")


def test_the_stand_thread_count_and_the_f32_switch_reach_the_compiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(openvino_runtime.THREADS_ENV, "3")
    monkeypatch.setenv(openvino_runtime.PRECISION_ENV, "F32")

    assert openvino_runtime.compile_config() == {
        "INFERENCE_NUM_THREADS": 3,
        "INFERENCE_PRECISION_HINT": "f32",
    }
    assert openvino_runtime.precision_name() == "f32"


@pytest.mark.parametrize("threads", ["", "0", "four"])
def test_no_usable_thread_count_leaves_the_engine_its_default(
    monkeypatch: pytest.MonkeyPatch, threads: str
) -> None:
    monkeypatch.setenv(openvino_runtime.THREADS_ENV, threads)
    monkeypatch.delenv(openvino_runtime.PRECISION_ENV, raising=False)

    assert (openvino_runtime.compile_config(), openvino_runtime.precision_name()) == ({}, "auto")


def test_the_real_import_finds_the_declining_consent(tmp_path: Path) -> None:
    """With the real package: the consent checker reads what the memory wrote."""
    pytest.importorskip("openvino_telemetry")
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import openvino_runtime; openvino_runtime.decline_telemetry();"
        "from openvino_telemetry.utils.opt_in_checker import OptInChecker, ConsentCheckResult;"
        "print(OptInChecker().check(enable_opt_in_dialog=False) == ConsentCheckResult.DECLINED)"
    )
    environment = {**os.environ, "HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path)}

    done = subprocess.run(
        [sys.executable, "-c", script, str(SCRIPTS)],
        env=environment, capture_output=True, text=True, timeout=LONG_TIMEOUT, check=True,
    )

    assert done.stdout.strip() == "True"


def test_doctor_names_a_consent_that_is_not_declined(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import doctor
    import install_models

    monkeypatch.setattr(install_models, "wanted_models", install_models.pinned_models)
    monkeypatch.setattr(install_models, "missing_models", lambda: [])
    consent = openvino_runtime.telemetry_consent_file()
    consent.parent.mkdir(parents=True)
    consent.write_text("1\n", encoding="utf-8")

    accepted = doctor._models_check()
    consent.write_text("0\n", encoding="utf-8")
    declined = doctor._models_check()

    assert (accepted["status"], str(consent) in accepted["message"]) == ("degraded", True)
    assert (declined["status"], declined["details"]["telemetry_declined"]) == ("ok", True)
