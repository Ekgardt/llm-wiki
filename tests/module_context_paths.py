"""One contained-path primitive for maintenance context and record catalogs."""
from pathlib import Path


def _contained(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    path.relative_to(root.resolve())
    return path
