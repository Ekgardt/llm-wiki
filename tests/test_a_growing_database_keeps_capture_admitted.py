"""SQLite admission validates identity/schema without a whole-file read budget."""
from pathlib import Path

import pytest
from installed_memory_repair import ReliabilityV3ValidationError, require_reliability_v3_admission

from tests.test_reliability_v3_adoption import _vault, build_adopted_reliability_v3


@pytest.mark.parametrize("name", ["queue-v3.sqlite3", "markdown-transactions-v3.sqlite3"])
def test_a_database_past_the_old_health_read_budget_is_still_admitted(tmp_path: Path, name: str):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    path = state / "run" / name
    with path.open("r+b") as handle:
        handle.truncate(256 * 1024 * 1024 + 4096)
    assert require_reliability_v3_admission(root=root, state_root=state)["schema_version"] == "reliability-v3-adoption/v1"


@pytest.mark.parametrize("unsafe", ["symlink", "permissions"])
def test_size_independent_admission_keeps_the_path_and_permission_checks(tmp_path: Path, unsafe: str):
    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    path = state / "run/queue-v3.sqlite3"
    if unsafe == "permissions":
        path.chmod(0o644)
    if unsafe == "symlink":
        target = path.with_suffix(".retained")
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises(ReliabilityV3ValidationError, match="runtime file must"):
        require_reliability_v3_admission(root=root, state_root=state)


def test_explicit_whole_file_budgets_are_still_enforced(tmp_path: Path):
    from reliable_memory import open_readonly_operational_db

    root, state = _vault(tmp_path)
    build_adopted_reliability_v3(root, state)
    with pytest.raises(PermissionError, match="bounded regular file"):
        open_readonly_operational_db(state / "run/queue-v3.sqlite3", state, max_bytes=1)
