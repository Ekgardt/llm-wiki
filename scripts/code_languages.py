"""Dependency-free code language detection shared by corpus and graph collectors."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import PurePath
from types import MappingProxyType

CODE_LANGUAGE_BY_SUFFIX: Mapping[str, str] = MappingProxyType(
    {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".hh": "cpp",
        ".hpp": "cpp",
        ".hxx": "cpp",
        ".rb": "ruby",
        ".php": "php",
        ".cs": "c_sharp",
        ".sh": "bash",
        ".bash": "bash",
    }
)
CLASSIFIER_VERSION = "code-language-classifier/v1"
CLASSIFIER_MAP_SHA256 = hashlib.sha256(
    json.dumps(
        dict(CODE_LANGUAGE_BY_SUFFIX),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
).hexdigest()
CLASSIFIER_IDENTITY = f"{CLASSIFIER_VERSION}+sha256:{CLASSIFIER_MAP_SHA256}"


def language_for_path(path: PurePath) -> str | None:
    """Return the code language for a path's case-insensitive final suffix."""
    return CODE_LANGUAGE_BY_SUFFIX.get(path.suffix.casefold())
