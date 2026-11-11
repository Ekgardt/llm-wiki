"""The operator corpus root the command line accepts is the one the no-follow traversal reads."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parent.parent / "benchmark"
SCRIPTS_ROOT = Path(__file__).resolve().parent.parent / "scripts"
for path in (BENCHMARK_ROOT, SCRIPTS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import navigation_benchmark_operator_corpus as operator_corpus  # noqa: E402
from run_code_navigation import parse_args  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def test_parse_args_preserves_absolute_operator_path_for_no_follow_traversal(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "operator-lexical").absolute()
    root.mkdir()
    (root / "inside.py").write_text("def inside():\n    return 1\n", encoding="utf-8")

    args = parse_args(
        ["--fixture", "--correctness-only", "--operator-corpus", os.fspath(root)]
    )
    expected = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))

    assert args.operator_corpus == expected
    traversed_root, files = operator_corpus._operator_python_files(
        args.operator_corpus,
        deadline=time.monotonic() + SHORT_TIMEOUT,
    )
    assert traversed_root == root.resolve()
    assert [path.name for path in files] == ["inside.py"]


def test_parse_and_traversal_reject_operator_root_symlink_without_leaking_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "PRIVATE_ROOT_TARGET"
    target.mkdir()
    (target / "secret.py").write_text("PRIVATE_ROOT_TEXT", encoding="utf-8")
    link = tmp_path / "operator-root-link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {type(exc).__name__}")

    with pytest.raises(SystemExit):
        parse_args(
            ["--fixture", "--correctness-only", "--operator-corpus", os.fspath(link)]
        )
    captured = capsys.readouterr()
    with pytest.raises(RuntimeError, match="regular directory"):
        operator_corpus._operator_python_files(
            link,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

    assert target.name not in captured.err
    assert "PRIVATE_ROOT_TEXT" not in captured.err


@pytest.mark.skipif(os.name != "nt", reason="Windows root-junction coverage")
def test_parse_and_traversal_reject_operator_root_windows_junction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "PRIVATE_JUNCTION_TARGET"
    target.mkdir()
    junction = tmp_path / "operator-root-junction"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        shell=False,
        timeout=10,
    )
    if created.returncode != 0:
        pytest.skip("junction creation is unavailable")

    with pytest.raises(SystemExit):
        parse_args(
            ["--fixture", "--correctness-only", "--operator-corpus", os.fspath(junction)]
        )
    captured = capsys.readouterr()
    with pytest.raises(RuntimeError, match="regular directory"):
        operator_corpus._operator_python_files(
            junction,
            deadline=time.monotonic() + SHORT_TIMEOUT,
        )

    assert target.name not in captured.err
