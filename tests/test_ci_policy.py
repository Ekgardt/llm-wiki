from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
ACTION_PINS = {
    "actions/checkout": "11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/setup-node": "49933ea5288caeca8642d1e84afbd3f7d6820020",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "astral-sh/setup-uv": "d4b2f3b6ecc6e67c4457f6d3e41ec42d3d0fcb86",
    "gitleaks/gitleaks-action": "e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e",
}
RUNNERS = {"ubuntu-24.04", "windows-2025", "macos-15"}


def _workflow() -> dict[str, object]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _commands(job: dict[str, object]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job["steps"])


def test_every_external_action_is_full_sha_pinned_with_release_comment() -> None:
    workflow = _workflow()
    source_lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    uses = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            value = step.get("uses")
            if value:
                uses.append(value)

    assert uses
    for value in uses:
        owner, revision = value.rsplit("@", 1)
        assert re.fullmatch(r"[0-9a-f]{40}", revision), value
        assert revision == ACTION_PINS[owner], value
        matching_lines = [line for line in source_lines if value in line]
        assert matching_lines and all(re.search(r"# v\d", line) for line in matching_lines)


def test_every_checkout_drops_credentials_and_uv_is_exactly_pinned() -> None:
    checkout_steps = []
    setup_uv_steps = []
    for job in _workflow()["jobs"].values():
        for step in job.get("steps", []):
            value = str(step.get("uses", ""))
            if value.startswith("actions/checkout@"):
                checkout_steps.append(step)
            if value.startswith("astral-sh/setup-uv@"):
                setup_uv_steps.append(step)

    assert checkout_steps and setup_uv_steps
    assert all(step.get("with", {}).get("persist-credentials") is False for step in checkout_steps)
    assert all(step["with"] == {"version": "0.12.3", "enable-cache": True} for step in setup_uv_steps)


def test_every_runner_is_an_explicit_supported_generation() -> None:
    for job in _workflow()["jobs"].values():
        runner = job["runs-on"]
        if isinstance(runner, str) and runner.startswith("${{"):
            matrix_values = {entry["os"] for entry in job["strategy"]["matrix"]["include"]}
            assert matrix_values <= RUNNERS
        elif isinstance(runner, str) and runner.startswith("${{") is False:
            assert runner in RUNNERS
        else:
            raise AssertionError(f"unsupported runner expression: {runner!r}")
    assert "-latest" not in WORKFLOW.read_text(encoding="utf-8")


def test_job_level_env_avoids_contexts_unavailable_before_runner_assignment() -> None:
    invalid = []
    for job_name, job in _workflow()["jobs"].items():
        for variable, value in job.get("env", {}).items():
            if "${{ runner." in str(value):
                invalid.append((job_name, variable, value))

    assert invalid == []


def test_full_suite_matrix_contains_every_supported_python_endpoint() -> None:
    job = _workflow()["jobs"]["pytest-full"]
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["timeout-minutes"] == "${{ matrix.timeout }}"
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["include"] == [
        {"os": "ubuntu-24.04", "python": "3.10", "timeout": 45, "class": "linux_full"},
        {"os": "ubuntu-24.04", "python": "3.14", "timeout": 45, "class": "linux_full"},
        {"os": "windows-2025", "python": "3.10", "timeout": 60, "class": "windows_full"},
        {"os": "windows-2025", "python": "3.11", "timeout": 60, "class": "windows_full"},
        {"os": "windows-2025", "python": "3.12", "timeout": 60, "class": "windows_full"},
        {"os": "windows-2025", "python": "3.13", "timeout": 60, "class": "windows_full"},
        {"os": "windows-2025", "python": "3.14", "timeout": 60, "class": "windows_full"},
        {"os": "macos-15", "python": "3.10", "timeout": 45, "class": "macos_full"},
        {"os": "macos-15", "python": "3.14", "timeout": 45, "class": "macos_full"},
    ]


def test_clean_profiles_are_isolated_and_never_auto_sync() -> None:
    jobs = _workflow()["jobs"]
    environments = set()
    for name, extra in (
        ("clean-production", None),
        ("clean-hybrid", "hybrid"),
        ("clean-code-graph", "code-graph"),
    ):
        job = jobs[name]
        assert job["runs-on"] == "ubuntu-24.04"
        assert job["timeout-minutes"] == 20
        assert job["strategy"]["matrix"]["python"] == ["3.10", "3.14"]
        environment = job["env"]["UV_PROJECT_ENVIRONMENT"]
        assert environment not in environments
        environments.add(environment)
        commands = _commands(job)
        assert "uv sync --locked --no-default-groups" in commands
        if extra is not None:
            assert f"--extra {extra}" in commands
        for line in commands.splitlines():
            if line.strip().startswith("uv run"):
                assert "--locked --no-sync" in line

    production = _commands(jobs["clean-production"])
    assert "install_smoke.py" in production
    assert "find_spec('pytest') is None" in production
    assert "Path(os.environ['LLM_WIKI_STATE_ROOT']).mkdir(parents=True, exist_ok=True)" in production
    hybrid = _commands(jobs["clean-hybrid"])
    assert "find_spec('pytest') is None" in hybrid
    assert "np.save" in hybrid and "np.load" in hybrid
    code_graph = _commands(jobs["clean-code-graph"])
    assert "find_spec('pytest') is None" in code_graph
    assert "scripts/code_graph.py tests/fixtures/code_kernel/python" in code_graph


def test_native_installer_matrix_covers_linux_and_windows() -> None:
    job = _workflow()["jobs"]["installer"]
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["timeout-minutes"] == 20
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["include"] == [
        {"os": "ubuntu-24.04", "platform": "linux"},
        {"os": "windows-2025", "platform": "windows"},
    ]
    commands = _commands(job)
    for path in (
        "tests/test_installer_bootstrap.py",
        "tests/test_installer_config.py",
        "tests/test_install_smoke.py",
        "tests/test_integration_injection.py",
        "tests/test_dependency_environments.py",
    ):
        assert path in commands


def test_job_timeouts_names_and_timing_artifacts_match_approved_classes() -> None:
    jobs = _workflow()["jobs"]
    assert jobs["lint"]["timeout-minutes"] == 15
    assert jobs["lint"]["name"] == "timing::focused::lint"
    assert jobs["pyright-navigation"]["timeout-minutes"] == 15
    assert jobs["pyright-navigation"]["name"].startswith("timing::focused::")
    assert jobs["installer"]["name"] == "timing::installer::${{ matrix.platform }}"
    for name in ("clean-production", "clean-hybrid", "clean-code-graph"):
        assert jobs[name]["name"].startswith("timing::clean::")

    full = jobs["pytest-full"]
    assert full["name"] == "timing::${{ matrix.class }}::py${{ matrix.python }}"
    commands = _commands(full)
    assert "--junitxml" in commands and "--durations=0" in commands
    upload = [
        step
        for step in full["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(upload) == 1
    assert upload[0]["if"] == "always()"
    assert upload[0]["with"]["name"] == (
        "pytest-timings-${{ matrix.class }}-py${{ matrix.python }}-"
        "attempt-${{ github.run_attempt }}"
    )


def test_ruff_covers_scripts_tests_and_benchmark() -> None:
    assert "ruff check scripts/ tests/ benchmark/" in _commands(_workflow()["jobs"]["lint"])


def test_compileall_excludes_the_intentionally_invalid_parser_fixture() -> None:
    commands = _commands(_workflow()["jobs"]["lint"])
    assert (
        "python -m compileall -q -x "
        '"tests[\\\\/]fixtures[\\\\/]code_kernel[\\\\/]python[\\\\/]pkg[\\\\/]broken[.]py" '
        "scripts tests benchmark"
    ) in commands


def test_pyright_state_passing_is_shell_independent() -> None:
    job = _workflow()["jobs"]["pyright-navigation"]
    install = next(step for step in job["steps"] if step.get("name") == "Explicit Pyright install")
    assert job["env"]["LLM_WIKI_STATE_ROOT"] == (
        "${{ github.workspace }}/../llm-wiki-state"
    )
    assert '"${{ env.LLM_WIKI_STATE_ROOT }}"' in install["run"]
    assert "shell" not in install
    tests = next(
        step
        for step in job["steps"]
        if step.get("name") == "Protocol, process-tree, and security tests"
    )
    assert "LLM_WIKI_STATE_ROOT" not in tests.get("env", {})
    for name in ("Correctness benchmark gate", "Fixed 100 KLOC Python qualification gate"):
        step = next(step for step in job["steps"] if step.get("name") == name)
        assert "LLM_WIKI_STATE_ROOT" not in step.get("env", {})
