"""What the evidence-graph writer stores, stated once for the writer and its readers.

The writer refuses text with a NUL or a line break, text past its bound, and
metadata that is not canonical JSON; each refusal fails a whole generation. The
extractors in front of it make their records storable with these same rules, so a
23 569-character signature, a route path with a line break or `status: 2026-09-01`
cost nothing. No imports beyond the standard library: the code extractor is
importable as a package. See
`docs/research/2026-09-14-the-rest-of-the-readers-before-the-writer.md`.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping

MAX_IDENTITY_KEY_CHARS = 4096
_UNSTORABLE_CHARACTERS = frozenset("\x00\r\n")


def valid_graph_text(value: object, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= maximum
        and _UNSTORABLE_CHARACTERS.isdisjoint(value)
    )


def storable_identity_key(key: str) -> str:
    """The key when the writer stores it verbatim, else `sha256:` and its digest.

    The extractor derives the node id from the original key, so identities stay
    distinct and stable.
    """
    if valid_graph_text(key, MAX_IDENTITY_KEY_CHARS):
        return key
    return "sha256:" + hashlib.sha256(str(key).encode("utf-8", "surrogatepass")).hexdigest()


def _storable_text(value: str) -> str:
    return value.encode("utf-8", "replace").decode("utf-8")


def _storable_float(value: float) -> str | None:
    if not math.isfinite(value):
        return None
    return str(value)


def _dated_text(value: object) -> object:
    """A date or datetime as ISO text; anything else as it is."""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _storable_scalar(value: object) -> object:
    if isinstance(value, str):
        return _storable_text(value)
    if isinstance(value, float):
        return _storable_float(value)
    return _dated_text(value)


def storable_metadata(value: object) -> object:
    """Node metadata in the canonical JSON the writer stores.

    Dates become ISO text, floats their decimal text (non-finite ones nothing),
    lone surrogates the replacement character.
    """
    if isinstance(value, Mapping):
        return {str(key): storable_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [storable_metadata(item) for item in value]
    return _storable_scalar(value)
