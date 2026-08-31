"""The actor of a lease is the agent taking it, not the machine account.

Measured 2026-08-28 on the live vault: `maintenance_owners.actor_id` is UNIQUE
and `current_actor_identity()` returned `posix-uid:<uid>`, so exactly one
lease could exist per user. Holding `nightly/global` refused every other role,
including the compile the nightly pass spawns itself. Decision:
`knowledge/notes/ownership-actor-is-the-agent-decision.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import markdown_transaction  # noqa: E402
import operational_ownership as ownership  # noqa: E402

OTHER_ROLES = ("queue-worker", "capture", "markdown-writer", "project", "doctor")


def _registry(state_root: Path) -> ownership.OwnershipRegistry:
    (state_root / "run").mkdir(parents=True, exist_ok=True)
    candidate = state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"
    markdown_transaction.initialize_coordinator_v3_candidate(candidate, source_v2=None)
    markdown_transaction.MarkdownCoordinator._from_v3_candidate(  # noqa: SLF001
        candidate, state_root=state_root
    )
    return ownership.OwnershipRegistry(state_root)


def test_two_writers_of_different_scopes_coexist(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.acquire("markdown-writer", scope="project-alpha")
    second = registry.acquire("markdown-writer", scope="project-beta")
    assert second.epoch >= 1


@pytest.mark.parametrize("role", OTHER_ROLES)
def test_the_nightly_lease_blocks_no_other_role(tmp_path: Path, role: str) -> None:
    """The pass that owns the night must not lock out the steps it runs."""
    registry = _registry(tmp_path)
    ownership.acquire_scheduled_owner("nightly", state_root=tmp_path)
    assert registry.acquire(role, scope="scope-under-test").epoch >= 1


def test_the_nightly_pass_can_still_compile(tmp_path: Path) -> None:
    """`scheduled_nightly` spawns `maybe_compile` while holding its own lease."""
    _registry(tmp_path)
    ownership.acquire_scheduled_owner("nightly", state_root=tmp_path)
    lease, _marker = ownership.acquire_compile_owner(state_root=tmp_path)
    assert lease.role == "compile"


def test_one_owner_per_role_and_scope_still_holds(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.acquire("markdown-writer", scope="global")
    with pytest.raises(ownership.OperationalOwnershipError) as error:
        registry.acquire("markdown-writer", scope="global")
    assert error.value.code == "owner_busy"


def test_a_named_actor_still_may_not_hold_two_leases(tmp_path: Path) -> None:
    """An explicitly named actor keeps the single-lease rule it opted into."""
    registry = _registry(tmp_path)
    registry.acquire("queue-worker", scope="w1", actor_id="posix-uid:7")
    with pytest.raises(ownership.OperationalOwnershipError) as error:
        registry.acquire("capture", scope="s1", actor_id="posix-uid:7")
    assert error.value.code == "owner_identity_conflict"


def test_the_agent_identity_stays_inside_the_column_bound() -> None:
    """`actor_id` is bounded to 256 bytes; scope alone may reach 512."""
    actor = ownership.ownership_actor_identity("project", "s" * 512)
    assert len(actor.encode("utf-8")) <= 256


def test_two_roles_of_one_process_are_two_agents() -> None:
    first = ownership.ownership_actor_identity("nightly", "global")
    second = ownership.ownership_actor_identity("compile", "global")
    assert first != second
