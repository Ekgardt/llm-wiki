"""Which capture handler a retained intent was written for."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from capture_publication import CAPTURE_HANDLER_VERSION


def _verified_session_identity(state_root: Path, payload: bytes, *, deadline: float) -> str:
    from capture_intent_records import (
        decode_capture_intent,
        require_capture_intent_identity,
    )

    document = decode_capture_intent(payload)
    require_capture_intent_identity(document)
    return str(document["intent_id"])


def _verified_breadcrumb_identity(state_root: Path, payload: bytes, *, deadline: float) -> str:
    import breadcrumb_protocol as protocol
    from breadcrumb_bundle import (
        load_bundle,
    )

    bundle = load_bundle(state_root, payload, deadline=deadline)
    return str(protocol.read_manifest(bundle.manifest)["intent_id"])


def verified_capture_handler(
    state_root: Path, record: dict[str, Any], payload: bytes, *, deadline: float = float("inf"),
) -> int:
    from breadcrumb_bundle import (
        HANDLER_VERSION,
    )

    document = json.loads(payload)
    if not isinstance(document, dict):
        raise ValueError("capture intent must be an object")
    validators = {
        "capture-intent/v1": (CAPTURE_HANDLER_VERSION, _verified_session_identity),
        "capture-intent/v2": (HANDLER_VERSION, _verified_breadcrumb_identity),
    }
    version, validate = validators[document["schema_version"]]
    if validate(state_root, payload, deadline=deadline) != str(record["intent_id"]):
        raise ValueError("capture intent identity conflicts with its index")
    return version
