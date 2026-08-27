"""Fail-closed content protection for first-party model calls."""

from __future__ import annotations

import difflib
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
    allow_finding_fingerprints: frozenset[str] = frozenset()


def load_policy() -> DLPPolicy:
    """Load and authenticate the optional bounded external policy."""
    configured = os.environ.get("LLM_WIKI_DLP_POLICY")
    if configured is None:
        return DLPPolicy()
    path = Path(configured)
    if not path.is_absolute():
        raise DLPPolicyError("LLM_WIKI_DLP_POLICY must be an absolute path")
    document = _parsed_policy_document(_read_policy_bytes(path))
    return _validated_policy(document)


def _read_policy_bytes(path: Path) -> bytes:
    try:
        _require_regular_bounded_file(path)
        raw = path.read_bytes()
    except DLPPolicyError:
        raise
    except OSError as exc:
        raise DLPPolicyError("required DLP policy is unreadable") from exc
    if len(raw) > _MAX_POLICY_BYTES:
        raise DLPPolicyError("required DLP policy exceeds the size limit")
    return raw


def _require_regular_bounded_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise DLPPolicyError("required DLP policy is not a regular file")
    if path.stat().st_size > _MAX_POLICY_BYTES:
        raise DLPPolicyError("required DLP policy exceeds the size limit")


def _parsed_policy_document(raw: bytes) -> dict:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DLPPolicyError("required DLP policy is not valid UTF-8 JSON") from exc
    _require_policy_schema(document)
    return document


# The optional key extends the 2026-08-25 exact-content allowlist with
# per-finding entries; a policy without it keeps its original digest, so
# already-authenticated policies stay valid byte for byte.
_POLICY_REQUIRED_KEYS = frozenset({"version", "literals", "allow_fingerprints", "sha256"})
_POLICY_OPTIONAL_KEYS = frozenset({"allow_finding_fingerprints"})


def _policy_keys_valid(document: object) -> bool:
    return (
        isinstance(document, dict)
        and _POLICY_REQUIRED_KEYS <= set(document)
        and set(document) <= (_POLICY_REQUIRED_KEYS | _POLICY_OPTIONAL_KEYS)
    )


def _require_policy_schema(document: object) -> None:
    if not _policy_keys_valid(document):
        raise DLPPolicyError("required DLP policy has an invalid schema")
    if document["version"] != 1 or isinstance(document["version"], bool):
        raise DLPPolicyError("required DLP policy version is unsupported")


def _literals_valid(literals: object) -> bool:
    return (
        isinstance(literals, list)
        and len(literals) <= _MAX_LITERALS
        and all(_valid_literal(value) for value in literals)
    )


def _require_valid_literals(literals: object) -> None:
    if not _literals_valid(literals) or len(set(literals)) != len(literals):
        raise DLPPolicyError("required DLP policy literals are invalid")


def _valid_literal(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= _MAX_LITERAL_BYTES
    )


def _valid_fingerprint(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _fingerprints_valid(fingerprints: object) -> bool:
    return (
        isinstance(fingerprints, list)
        and len(fingerprints) <= _MAX_ALLOW_FINGERPRINTS
        and all(_valid_fingerprint(value) for value in fingerprints)
    )


def _require_valid_fingerprints(fingerprints: object) -> None:
    if not _fingerprints_valid(fingerprints) or len(set(fingerprints)) != len(fingerprints):
        raise DLPPolicyError("required DLP policy fingerprints are invalid")


def _digest_payload(document: dict) -> dict:
    """The canonical payload; the optional key stays out of pre-extension digests."""
    payload = {
        "version": 1,
        "literals": document["literals"],
        "allow_fingerprints": document["allow_fingerprints"],
    }
    if "allow_finding_fingerprints" in document:
        payload["allow_finding_fingerprints"] = document["allow_finding_fingerprints"]
    return payload


def _require_authentic_digest(document: dict) -> None:
    digest = document["sha256"]
    if not _valid_fingerprint(digest):
        raise DLPPolicyError("required DLP policy digest is invalid")
    payload = _digest_payload(document)
    actual_digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if not hmac.compare_digest(digest, actual_digest):
        raise DLPPolicyError("required DLP policy digest does not match")


def _validated_policy(document: dict) -> DLPPolicy:
    findings = document.get("allow_finding_fingerprints", [])
    _require_valid_literals(document["literals"])
    _require_valid_fingerprints(document["allow_fingerprints"])
    _require_valid_fingerprints(findings)
    _require_authentic_digest(document)
    return DLPPolicy(
        tuple(document["literals"]),
        frozenset(document["allow_fingerprints"]),
        frozenset(findings),
    )


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _scrubbed(text: str, policy: DLPPolicy) -> str:
    """The text with every built-in finding and configured literal replaced."""
    redacted = redact_secrets(text)
    for literal in policy.literals:
        redacted = redacted.replace(literal, "[REDACTED_LITERAL]")
    return redacted


def redact_for_transport(text: str, policy: DLPPolicy) -> str:
    """Redact built-in findings and configured literals before transport."""
    if _fingerprint(text) in policy.allow_fingerprints:
        return text
    return _scrubbed(text, policy)


def _finding_spans(text: str, scrubbed: str) -> list[str]:
    """The exact input spans the scrubber replaced, recovered by alignment.

    The redactor exposes no findings API, so the spans are read off the diff
    between the input and its scrubbed form — the same shape detect-secrets
    stores in its baseline: the secret value itself, hashed. Misalignment can
    only shrink or shift a span, which changes its hash and therefore fails
    closed; it can never widen the allowlist.
    """
    matcher = difflib.SequenceMatcher(None, text, scrubbed, autojunk=False)
    spans = [
        text[start:end]
        for tag, start, end, _, _ in matcher.get_opcodes()
        if tag in ("replace", "delete")
    ]
    return [span for span in spans if span]


def _findings_allowlisted(text: str, scrubbed: str, policy: DLPPolicy) -> bool:
    """Every replaced span is individually allowlisted, and there is at least one.

    This is the per-finding unlock the 2026-08-25 decision left to the owner:
    an edit elsewhere in the same file no longer re-blocks it, while a single
    new finding — one span whose hash is not in the list — still blocks.
    """
    if not policy.allow_finding_fingerprints:
        return False
    spans = _finding_spans(text, scrubbed)
    if not spans:
        return False
    return all(
        _fingerprint(span) in policy.allow_finding_fingerprints for span in spans
    )


def _redact_transport_key(key: object, policy: DLPPolicy) -> object:
    return redact_for_transport(key, policy) if isinstance(key, str) else key


def _redact_transport_mapping(value: Mapping, policy: DLPPolicy) -> dict:
    return {
        _redact_transport_key(key, policy): redact_transport_value(item, policy)
        for key, item in value.items()
    }


def _redact_transport_sequence(value: object, policy: DLPPolicy) -> object:
    if isinstance(value, list):
        return [redact_transport_value(item, policy) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_transport_value(item, policy) for item in value)
    return value


def redact_transport_value(value: object, policy: DLPPolicy) -> object:
    """Redact strings recursively in a JSON-like provider argument."""
    if isinstance(value, str):
        return redact_for_transport(value, policy)
    if isinstance(value, Mapping):
        return _redact_transport_mapping(value, policy)
    return _redact_transport_sequence(value, policy)


def require_safe_model_output(text: str, policy: DLPPolicy) -> None:
    """Reject sensitive provider output unless the payload or every finding is allowlisted."""
    if _fingerprint(text) in policy.allow_fingerprints:
        return
    scrubbed = _scrubbed(text, policy)
    if scrubbed != text and not _findings_allowlisted(text, scrubbed, policy):
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
