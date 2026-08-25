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


def _step_uses() -> list[str]:
    return [
        str(step["uses"])
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if step.get("uses")
    ]


def _steps_using(prefix: str) -> list[dict[str, object]]:
    return [
        step
        for job in _workflow()["jobs"].values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith(prefix)
    ]


def _assert_pinned_by_sha(value: str) -> None:
    owner, revision = value.rsplit("@", 1)
    assert re.fullmatch(r"[0-9a-f]{40}", revision), value
    assert revision == ACTION_PINS[owner], value


def _assert_release_comment(value: str, source_lines: list[str]) -> None:
    matching = [line for line in source_lines if value in line]
    assert matching and all(re.search(r"# v\d", line) for line in matching)


def test_every_external_action_is_full_sha_pinned_with_release_comment() -> None:
    source_lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    uses = _step_uses()

    assert uses
    for value in uses:
        _assert_pinned_by_sha(value)
        _assert_release_comment(value, source_lines)


def test_every_checkout_drops_credentials_and_uv_is_exactly_pinned() -> None:
    checkout_steps = _steps_using("actions/checkout@")
    setup_uv_steps = _steps_using("astral-sh/setup-uv@")

    assert checkout_steps and setup_uv_steps
    assert all(step.get("with", {}).get("persist-credentials") is False for step in checkout_steps)
    assert all(step["with"] == {"version": "0.12.3", "enable-cache": True} for step in setup_uv_steps)


def _runner_values(job: dict[str, object]) -> set[str]:
    """Every runner a job can land on: one literal, or the matrix it expands to."""
    runner = job["runs-on"]
    if not isinstance(runner, str):
        raise AssertionError(f"unsupported runner expression: {runner!r}")
    if not runner.startswith("${{"):
        return {runner}
    return {entry["os"] for entry in job["strategy"]["matrix"]["include"]}


def test_every_runner_is_an_explicit_supported_generation() -> None:
    for job in _workflow()["jobs"].values():
        assert _runner_values(job) <= RUNNERS
    assert "-latest" not in WORKFLOW.read_text(encoding="utf-8")


def test_job_level_env_avoids_contexts_unavailable_before_runner_assignment() -> None:
    invalid = [
        (job_name, variable, value)
        for job_name, job in _workflow()["jobs"].items()
        for variable, value in job.get("env", {}).items()
        if "${{ runner." in str(value)
    ]

    assert invalid == []


SHARD_COUNT = 4


def test_full_suite_matrix_contains_every_supported_python_endpoint() -> None:
    job = _workflow()["jobs"]["pytest-full"]
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job["timeout-minutes"] == "${{ matrix.timeout }}"
    assert job["strategy"]["fail-fast"] is False
    assert job["strategy"]["matrix"]["include"] == [
        {
            "target": "linux-py3.10",
            "os": "ubuntu-24.04",
            "python": "3.10",
            "timeout": 20,
            "class": "linux_full",
        },
        {
            "target": "linux-py3.14",
            "os": "ubuntu-24.04",
            "python": "3.14",
            "timeout": 20,
            "class": "linux_full",
        },
        {
            "target": "windows-py3.10",
            "os": "windows-2025",
            "python": "3.10",
            "timeout": 40,
            "class": "windows_full",
        },
        {
            "target": "windows-py3.11",
            "os": "windows-2025",
            "python": "3.11",
            "timeout": 40,
            "class": "windows_full",
        },
        {
            "target": "windows-py3.12",
            "os": "windows-2025",
            "python": "3.12",
            "timeout": 40,
            "class": "windows_full",
        },
        {
            "target": "windows-py3.13",
            "os": "windows-2025",
            "python": "3.13",
            "timeout": 40,
            "class": "windows_full",
        },
        {
            "target": "windows-py3.14",
            "os": "windows-2025",
            "python": "3.14",
            "timeout": 40,
            "class": "windows_full",
        },
        {
            "target": "macos-py3.10",
            "os": "macos-15",
            "python": "3.10",
            "timeout": 20,
            "class": "macos_full",
        },
        {
            "target": "macos-py3.14",
            "os": "macos-15",
            "python": "3.14",
            "timeout": 20,
            "class": "macos_full",
        },
    ]


def test_every_full_suite_target_runs_all_four_shards() -> None:
    """Each target is one runner per shard, and the run step selects its own."""
    job = _workflow()["jobs"]["pytest-full"]
    matrix = job["strategy"]["matrix"]
    assert matrix["shard"] == list(range(1, SHARD_COUNT + 1))
    assert [entry["target"] for entry in matrix["include"]] == matrix["target"]
    assert job["name"].endswith("-s${{ matrix.shard }}")
    commands = _commands(job)
    assert "python -m tests.shard_plan" in commands
    assert f"--shard ${{{{ matrix.shard }}}} --of {SHARD_COUNT}" in commands
    assert "-s${{ matrix.shard }}" in job["env"]["LLM_WIKI_STATE_ROOT"]


def test_the_shards_cover_every_test_file_exactly_once() -> None:
    from tests import shard_plan

    shards = shard_plan.plan(SHARD_COUNT)
    selected = [name for shard in shards for name in shard]
    assert sorted(selected) == shard_plan.test_files()
    assert len(selected) == len(set(selected))
    assert all(shard for shard in shards)


CLEAN_PROFILES = (
    ("clean-production", None),
    ("clean-hybrid", "hybrid"),
    ("clean-code-graph", "code-graph"),
)


def _assert_clean_profile(job: dict[str, object], extra: str | None) -> None:
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == 20
    assert job["strategy"]["matrix"]["python"] == ["3.10", "3.14"]
    commands = _commands(job)
    assert "uv sync --locked --no-default-groups" in commands
    assert extra is None or f"--extra {extra}" in commands
    runs = [line for line in commands.splitlines() if line.strip().startswith("uv run")]
    assert all("--locked --no-sync" in line for line in runs)


def test_clean_profiles_are_isolated_and_never_auto_sync() -> None:
    jobs = _workflow()["jobs"]
    for name, extra in CLEAN_PROFILES:
        _assert_clean_profile(jobs[name], extra)
    environments = {jobs[name]["env"]["UV_PROJECT_ENVIRONMENT"] for name, _extra in CLEAN_PROFILES}
    assert len(environments) == len(CLEAN_PROFILES)

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
    assert jobs["pyright-navigation"]["timeout-minutes"] == "${{ matrix.timeout }}"
    assert jobs["pyright-navigation"]["name"].startswith("timing::focused::")
    assert jobs["installer"]["name"] == "timing::installer::${{ matrix.platform }}"
    for name in ("clean-production", "clean-hybrid", "clean-code-graph"):
        assert jobs[name]["name"].startswith("timing::clean::")

    full = jobs["pytest-full"]
    assert full["name"] == "timing::${{ matrix.class }}::py${{ matrix.python }}-s${{ matrix.shard }}"
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
        "pytest-timings-${{ matrix.class }}-py${{ matrix.python }}-s${{ matrix.shard }}-"
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


def _named_step(job: dict[str, object], name: str) -> dict[str, object]:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_pyright_state_passing_is_shell_independent() -> None:
    job = _workflow()["jobs"]["pyright-navigation"]
    install = _named_step(job, "Explicit Pyright install")
    assert job["env"]["LLM_WIKI_STATE_ROOT"] == (
        "${{ github.workspace }}/../llm-wiki-state"
    )
    assert '"${{ env.LLM_WIKI_STATE_ROOT }}"' in install["run"]
    assert "shell" not in install
    inherit_state_from_the_job = (
        "Protocol, process-tree, and security tests",
        "Correctness benchmark gate",
        "Fixed 100 KLOC Python qualification gate",
    )
    for name in inherit_state_from_the_job:
        assert "LLM_WIKI_STATE_ROOT" not in _named_step(job, name).get("env", {})


def test_one_aggregate_check_depends_on_every_other_job() -> None:
    """Branch protection requires names, so exactly one name must cover them all.

    Requiring forty-nine matrix names by hand fails the day the matrix changes:
    a new job is simply not required, and a red one merges. This binds the
    aggregate to the job list itself, so adding a job without adding it to
    `needs` fails here instead of silently leaving a hole in the gate.
    """
    jobs = _workflow()["jobs"]
    aggregate = jobs["all-green"]

    assert aggregate["name"] == "all-green"
    assert aggregate["if"] == "always()"
    assert set(aggregate["needs"]) == set(jobs) - {"all-green"}
    # A dependency that is skipped or cancelled must fail this check, not pass
    # it: `always()` runs the job, and only an explicit success comparison
    # keeps a non-success result from merging.
    commands = _commands(aggregate)
    # Single quotes are not cosmetic: a double-quoted string in a GitHub
    # expression makes the workflow file unparsable, and then nothing runs.
    assert "join(needs.*.result, ' ')" in commands
    assert '"${result}" = "success"' in commands
    assert "exit 1" in commands


def test_no_expression_uses_a_double_quoted_string() -> None:
    """A double-quoted string inside `${{ }}` makes the whole file unparsable.

    Not a style rule. GitHub expressions accept single-quoted strings only;
    one double quote and the workflow fails to load, so zero jobs run and the
    aggregate check never reports. That is exactly how `f64df64` reached main
    red: the run existed, lasted zero seconds, and had no jobs to fail.
    """
    offenders = [
        line
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if re.search(r"\$\{\{[^}]*\"", line)
    ]

    assert offenders == []
