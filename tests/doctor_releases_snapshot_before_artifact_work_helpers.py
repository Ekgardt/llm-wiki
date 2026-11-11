"""Helpers shared by test_doctor_releases_snapshot_before_artifact_work.py and the test files importing them."""

from markdown_transaction import active_markdown_coordinator

from tests.adopted_vault import adopt


def _environment(tmp_path):
    root, state = adopt(tmp_path)
    (root / "knowledge/notes").mkdir(parents=True, exist_ok=True)
    coordinator = active_markdown_coordinator(root, state)
    registry = coordinator._ownership_registry()
    lease = registry.acquire("doctor", scope="global", actor_id="snapshot-test", token="snapshot-token")
    return root, state, coordinator, registry, lease
