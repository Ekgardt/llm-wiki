"""Fail-closed content protection for first-party model calls."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from reliable_memory import canonical_json_bytes
from secret_redact import redact_secrets

_MAX_POLICY_BYTES = 256 * 1024
_MAX_LITERALS = 128
_MAX_LITERAL_BYTES = 2048
_MAX_ALLOW_FINGERPRINTS = 256
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class DLPPolicyError(ValueError):
    """The required external DLP policy could not be trusted."""


class DLPContentBlocked(ValueError):
    """Sensitive model output must not cross the durable boundary."""


@dataclass(frozen=True)
class DLPPolicy:
    literals: tuple[str, ...] = ()
    allow_fingerprints: frozenset[str] = frozenset()


def load_policy() -> DLPPolicy:
    """Load and authenticate the optional bounded external policy."""
    configured = os.environ.get("LLM_WIKI_DLP_POLICY")
    if configured is None:
        return DLPPolicy()
    path = Path(configured)
    if not path.is_absolute():
        raise DLPPolicyError("LLM_WIKI_DLP_POLICY must be an absolute path")
    try:
        if path.is_symlink() or not path.is_file():
            raise DLPPolicyError("required DLP policy is not a regular file")
        if path.stat().st_size > _MAX_POLICY_BYTES:
            raise DLPPolicyError("required DLP policy exceeds the size limit")
        raw = path.read_bytes()
    except DLPPolicyError:
        raise
    except OSError as exc:
        raise DLPPolicyError("required DLP policy is unreadable") from exc
    if len(raw) > _MAX_POLICY_BYTES:
        raise DLPPolicyError("required DLP policy exceeds the size limit")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DLPPolicyError("required DLP policy is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "version",
        "literals",
        "allow_fingerprints",
        "sha256",
    }:
        raise DLPPolicyError("required DLP policy has an invalid schema")
    if document["version"] != 1 or isinstance(document["version"], bool):
        raise DLPPolicyError("required DLP policy version is unsupported")
    literals = document["literals"]
    fingerprints = document["allow_fingerprints"]
    digest = document["sha256"]
    if (
        not isinstance(literals, list)
        or len(literals) > _MAX_LITERALS
        or any(
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > _MAX_LITERAL_BYTES
            for value in literals
        )
        or len(set(literals)) != len(literals)
    ):
        raise DLPPolicyError("required DLP policy literals are invalid")
    if (
        not isinstance(fingerprints, list)
        or len(fingerprints) > _MAX_ALLOW_FINGERPRINTS
        or any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in fingerprints
        )
        or len(set(fingerprints)) != len(fingerprints)
    ):
        raise DLPPolicyError("required DLP policy fingerprints are invalid")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise DLPPolicyError("required DLP policy digest is invalid")
    payload = {
        "version": 1,
        "literals": literals,
        "allow_fingerprints": fingerprints,
    }
    actual_digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if not hmac.compare_digest(digest, actual_digest):
        raise DLPPolicyError("required DLP policy digest does not match")
    return DLPPolicy(tuple(literals), frozenset(fingerprints))


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def redact_for_transport(text: str, policy: DLPPolicy) -> str:
    """Redact built-in findings and configured literals before transport."""
    if _fingerprint(text) in policy.allow_fingerprints:
        return text
    redacted = redact_secrets(text)
    for literal in policy.literals:
        redacted = redacted.replace(literal, "[REDACTED_LITERAL]")
    return redacted


def redact_transport_value(value: object, policy: DLPPolicy) -> object:
    """Redact strings recursively in a JSON-like provider argument."""
    if isinstance(value, str):
        return redact_for_transport(value, policy)
    if isinstance(value, Mapping):
        return {
            redact_for_transport(key, policy)
            if isinstance(key, str)
            else key: redact_transport_value(item, policy)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_transport_value(item, policy) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_transport_value(item, policy) for item in value)
    return value


def require_safe_model_output(text: str, policy: DLPPolicy) -> None:
    """Reject sensitive provider output unless its exact payload is allowlisted."""
    if _fingerprint(text) in policy.allow_fingerprints:
        return
    if redact_secrets(text) != text or any(literal in text for literal in policy.literals):
        raise DLPContentBlocked("model output contains protected content")


def require_safe_content(content: bytes, policy: DLPPolicy) -> None:
    """Reject protected bytes unless the complete payload is allowlisted."""
    if hashlib.sha256(content).hexdigest() in policy.allow_fingerprints:
        return
    try:
        text = content.decode("utf-8", errors="surrogateescape")
        require_safe_model_output(text, policy)
    except DLPContentBlocked as exc:
        raise DLPContentBlocked("content contains protected data") from exc
    except Exception as exc:  # noqa: BLE001 - scanner failure must block
        raise DLPContentBlocked("content scan failed") from exc


def require_safe_publication(content: bytes) -> None:
    """Recheck exact after-image bytes immediately before durable publication."""
    require_safe_content(content, load_policy())
