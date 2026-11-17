"""Safe error text and the redaction of navigation diagnostics."""
from __future__ import annotations

import re

from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES
from llm_wiki.mcp_server_limits import (
    MAX_MCP_ERROR_CHARS,
    MAX_MCP_EVIDENCE_BYTES,
    MAX_MCP_TOTAL_EVIDENCE_BYTES,
)
from secret_redact import redact_secrets


def _clamped_limit(value: int) -> tuple[int, bool]:
    effective = min(20, max(1, value))
    return effective, effective != value


def _degrade_quality(
    quality: dict,
    warning: str,
    *,
    coverage: float,
    confidence: float,
) -> dict:
    quality = quality.copy()
    quality["coverage"] = min(quality.get("coverage", 1.0), coverage)
    quality["confidence"] = min(quality.get("confidence", 1.0), confidence)
    quality["partial"] = True
    warnings = list(quality.get("warnings", []))
    if warning not in warnings:
        warnings.append(warning)
    quality["warnings"] = warnings
    return quality


def _record_tool_failure(operation: str, error: BaseException) -> None:
    """Leave the operator a trace of a failure the caller only sees a code for.

    The agent is told `operation_failed` and nothing else on purpose: exception
    text is untrusted and can carry paths or secrets. Dropping it entirely left
    nobody able to find out why, so it goes to the bounded, redacted failure
    trail that doctor already counts, under its own kind.
    """
    try:
        from capture_diagnostics import record_capture_failure

        record_capture_failure(
            "mcp_tool", f"{operation}: {type(error).__name__}: {error}", error=error
        )
    except Exception:  # noqa: BLE001 - diagnostics never break a tool call
        pass


def _safe_exception_text(error: BaseException, operation: str = "mcp") -> str:
    """Return a bounded stable code without exposing untrusted exception details."""
    _record_tool_failure(operation, error)
    code = "operation_timeout" if isinstance(error, TimeoutError) else "operation_failed"
    return " ".join(redact_secrets(code).split())[:MAX_MCP_ERROR_CHARS]


def _safe_page_read_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    allowed = {
        f"MCP page exceeds {MAX_KNOWLEDGE_PAGE_BYTES} bytes",
        "MCP page parent must be a regular directory",
        "MCP page must be a regular non-symlink file",
        "MCP page changed before open",
        "MCP page changed during read",
        "MCP page was replaced during read",
    }
    return message if message in allowed else _safe_exception_text(error)


def _safe_evidence_error(error: BaseException) -> str:
    message = " ".join(str(error).split())
    allowed = {
        f"evidence slice exceeds {MAX_MCP_EVIDENCE_BYTES} bytes",
        f"total evidence exceeds {MAX_MCP_TOTAL_EVIDENCE_BYTES} bytes",
    }
    return message if message in allowed else _safe_exception_text(error)


_ABSOLUTE_PATH = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/](?:[^\\/\s]+[\\/])*[^\\/\s]+|/(?:[^/\s]+/)+[^/\s]+)"
)
_NAVIGATION_REDACTION_MARKER = re.compile(
    r"\[(?:PATH|REDACTED(?:_[A-Z_]+)?)\]"
)


def _sanitized_diagnostic_text(value: str) -> str:
    text = _ABSOLUTE_PATH.sub("[REDACTED_PATH]", redact_secrets(value))
    return " ".join(text.split())[:MAX_MCP_ERROR_CHARS]


def _sanitized_diagnostic_list(value: list) -> list:
    return [_sanitize_diagnostic(item) for item in value]


def _sanitized_diagnostic_mapping(value: dict) -> dict:
    return {key: _sanitize_diagnostic(item) for key, item in value.items()}


def _sanitize_diagnostic(value):
    handlers = (
        (str, _sanitized_diagnostic_text),
        (list, _sanitized_diagnostic_list),
        (dict, _sanitized_diagnostic_mapping),
    )
    for kind, handler in handlers:
        if isinstance(value, kind):
            return handler(value)
    return value


def _sanitized_error_value(key: str, item):
    if key == "error":
        return _sanitize_diagnostic(item)
    return _sanitize_error_fields(item)


def _sanitize_error_fields(value):
    if isinstance(value, list):
        return [_sanitize_error_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _sanitized_error_value(key, item) for key, item in value.items()
        }
    return value


def sanitize_navigation_text(value: str) -> str:
    sanitized = redact_secrets(value)
    sanitized = _ABSOLUTE_PATH.sub("[PATH]", sanitized)
    original_bytes = len(value.encode("utf-8", errors="strict"))
    if len(sanitized.encode("utf-8", errors="strict")) <= original_bytes:
        return sanitized
    fitted = []
    used = 0
    index = 0
    while index < len(sanitized):
        marker = _NAVIGATION_REDACTION_MARKER.match(sanitized, index)
        token = marker.group() if marker is not None else sanitized[index]
        size = len(token.encode("utf-8", errors="strict"))
        if used + size > original_bytes:
            break
        fitted.append(token)
        used += size
        index += len(token)
    return "".join(fitted)


def _sanitize_navigation_location(value):
    if not isinstance(value, dict):
        return value
    sanitized = dict(value)
    signature = sanitized.get("signature")
    if isinstance(signature, str):
        sanitized["signature"] = sanitize_navigation_text(signature)
    return sanitized


def _sanitized_navigation_warning(item):
    if isinstance(item, str):
        return sanitize_navigation_text(item)
    return item


def _sanitize_navigation_warnings(sanitized: dict) -> None:
    warnings = sanitized.get("warnings")
    if not isinstance(warnings, (list, tuple)):
        return
    values = tuple(_sanitized_navigation_warning(item) for item in warnings)
    sanitized["warnings"] = values if isinstance(warnings, tuple) else list(values)


def _sanitize_navigation_hover(sanitized: dict) -> None:
    hover = sanitized.get("hover")
    if isinstance(hover, str):
        sanitized["hover"] = sanitize_navigation_text(hover)


def _sanitized_navigation_group(group):
    if not isinstance(group, dict):
        return group
    sanitized_group = dict(group)
    locations = group.get("locations")
    if isinstance(locations, list):
        sanitized_group["locations"] = [
            _sanitize_navigation_location(location) for location in locations
        ]
    return sanitized_group


def _sanitize_navigation_groups(sanitized: dict) -> None:
    groups = sanitized.get("groups")
    if isinstance(groups, list):
        sanitized["groups"] = [_sanitized_navigation_group(item) for item in groups]


def _sanitize_diagnostic_text_fields(diagnostic: dict, sanitized: dict) -> None:
    for field in ("message", "code"):
        value = diagnostic.get(field)
        if isinstance(value, str):
            sanitized[field] = sanitize_navigation_text(value)


def _sanitized_navigation_diagnostic(diagnostic):
    if not isinstance(diagnostic, dict):
        return diagnostic
    sanitized = dict(diagnostic)
    _sanitize_diagnostic_text_fields(diagnostic, sanitized)
    related = diagnostic.get("related")
    if isinstance(related, list):
        sanitized["related"] = [
            _sanitize_navigation_location(location) for location in related
        ]
    return sanitized


def _sanitize_navigation_diagnostics(sanitized: dict) -> None:
    diagnostics = sanitized.get("diagnostics")
    if isinstance(diagnostics, list):
        sanitized["diagnostics"] = [
            _sanitized_navigation_diagnostic(item) for item in diagnostics
        ]


def sanitize_navigation_data(data: dict) -> dict:
    sanitized = dict(data)
    _sanitize_navigation_warnings(sanitized)
    _sanitize_navigation_hover(sanitized)
    _sanitize_navigation_groups(sanitized)
    _sanitize_navigation_diagnostics(sanitized)
    return sanitized


_NAVIGATION_RENDER_KEYS = frozenset(
    {"requested_capability", "repository", "groups", "diagnostics"}
)


def _is_rendered_navigation_data(name: str, data) -> bool:
    if name != "get_architecture" or not isinstance(data, dict):
        return False
    if not isinstance(data.get("status"), str):
        return False
    return _NAVIGATION_RENDER_KEYS.issubset(data)
