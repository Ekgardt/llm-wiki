"""Strict existing byte-bounded UTF-8 text prefixes, independent of consumers."""
from __future__ import annotations


def utf8_prefix(value: str, ceiling: int) -> str:
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) <= ceiling:
        return value
    prefix = encoded[:ceiling]
    try:
        return prefix.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        return prefix[: exc.start].decode("utf-8", errors="strict")


# Preserve the renderer's existing function serialization identity.
utf8_prefix.__module__ = "code_navigation_renderer"
