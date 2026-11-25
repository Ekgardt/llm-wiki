"""Which files a call opens, so "it never read them" is checked instead of timed."""
from __future__ import annotations

import builtins
import io
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest


def record_opens_in(monkeypatch: pytest.MonkeyPatch, directory: Path) -> list[str]:
    """Record every file directly in `directory` that `os.open` or `open` opens."""
    opened: list[str] = []

    def recording(opener: Callable[..., Any]) -> Callable[..., Any]:
        def open_and_record(file: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(file, (str, os.PathLike)) and Path(file).parent == directory:
                opened.append(str(file))
            return opener(file, *args, **kwargs)

        return open_and_record

    monkeypatch.setattr(os, "open", recording(os.open))
    monkeypatch.setattr(io, "open", recording(io.open))
    monkeypatch.setattr(builtins, "open", recording(builtins.open))
    return opened
