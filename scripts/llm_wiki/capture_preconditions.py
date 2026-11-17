"""The fence a capture transaction must still hold when it publishes."""
from __future__ import annotations


def capture_transaction_preconditions(active: object, intent_fence: object) -> dict:
    return {
        "intent_fence": {
            "intent_id": intent_fence.intent_id,
            "mode": intent_fence.mode,
            "token": intent_fence.token,
            "fencing_epoch": intent_fence.epoch,
            "expires_at": intent_fence.expires_at.isoformat().replace("+00:00", "Z"),
        },
        "capture_binding": {
            "intent_id": active.intent_id,
            "task_id": active.task_id,
            "active_link_digest": active.active_digest,
            "seal_digest": active.seal_digest,
        },
    }
