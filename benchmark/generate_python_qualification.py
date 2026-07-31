"""Deterministic 100 KLOC Python qualification repository generator.

This module never invokes Git, Pyright, package installation, or the network.
It emits a deterministic, byte-stable repository totaling exactly 100,000
physical source lines plus a closed gold-query manifest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

FIXTURE_SEED = 411
FIXTURE_LINES = 100_000
MODULES = 1_000
LINES_PER_MODULE = FIXTURE_LINES // MODULES
PYRIGHT_VERSION = "1.1.411"


@dataclass(frozen=True, slots=True)
class GoldQuery:
    capability: str
    path: str
    line: int
    character: int
    expected_path: str
    expected_line: int


@dataclass(frozen=True, slots=True)
class QualificationRepository:
    root: Path
    line_count: int
    source_manifest_sha256: str
    gold_queries: tuple[GoldQuery, ...]
    ambiguous_symbols: tuple[str, ...]


def _module_lines(index: int) -> list[str]:
    name = f"{index:04d}"
    lines: list[str] = [
        f'"""Qualification module {name}."""',
        "from __future__ import annotations",
        "",
        f"class Service{name}:",
        f'    """Service number {index}."""',
        "",
        "    def __init__(self) -> None:",
        f"        self.identifier: int = {index}",
        "",
        "    def compute(self, factor: int) -> int:",
        "        return self.identifier * factor",
        "",
        f"def call_target_{name}(value: int) -> int:",
        f"    return value + {index}",
        "",
        "def execute(value: int) -> int:",
        f"    return value - {index}",
        "",
    ]
    pad = LINES_PER_MODULE - len(lines)
    for pad_index in range(pad):
        lines.append(f"# pad {name}:{pad_index}")
    return lines[:LINES_PER_MODULE]


def _write_gold_definition(index: int) -> GoldQuery:
    name = f"{index:04d}"
    return GoldQuery(
        capability="definition",
        path=f"pkg/mod_{name}.py",
        line=9,
        character=8,
        expected_path=f"pkg/mod_{name}.py",
        expected_line=4,
    )


def _write_gold_reference(index: int) -> GoldQuery:
    name = f"{index:04d}"
    return GoldQuery(
        capability="references",
        path=f"pkg/mod_{name}.py",
        line=13,
        character=4,
        expected_path=f"pkg/mod_{name}.py",
        expected_line=13,
    )


def _source_manifest_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\0")
    return digest.hexdigest()


def generate_qualification_repository(destination: Path) -> QualificationRepository:
    """Emit the deterministic qualification repository under ``destination``."""
    destination = Path(destination)
    pkg = destination / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (destination / "pyrightconfig.json").write_text(
        json.dumps(
            {
                "typeCheckingMode": "off",
                "reportMissingTypeStubs": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    total_lines = 0
    gold: list[GoldQuery] = []
    for index in range(MODULES):
        lines = _module_lines(index)
        content = "\n".join(lines) + "\n"
        (pkg / f"mod_{index:04d}.py").write_bytes(content.encode("utf-8"))
        total_lines += len(lines)
        if index < 200:
            gold.append(_write_gold_definition(index))
        if index < 100:
            gold.append(_write_gold_reference(index))
    manifest = _source_manifest_sha256(destination)
    return QualificationRepository(
        root=destination,
        line_count=total_lines,
        source_manifest_sha256=manifest,
        gold_queries=tuple(gold),
        ambiguous_symbols=("execute",),
    )


def write_gold_manifest(repository: QualificationRepository, path: Path) -> None:
    payload = {
        "fixture_seed": FIXTURE_SEED,
        "fixture_lines": repository.line_count,
        "source_manifest_sha256": repository.source_manifest_sha256,
        "gold_queries": [
            {
                "capability": query.capability,
                "path": query.path,
                "line": query.line,
                "character": query.character,
                "expected_path": query.expected_path,
                "expected_line": query.expected_line,
            }
            for query in repository.gold_queries
        ],
        "ambiguous_symbols": list(repository.ambiguous_symbols),
    }
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate the qualification repository")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--gold", type=Path, default=None)
    args = parser.parse_args()
    repository = generate_qualification_repository(args.destination)
    if args.gold is not None:
        write_gold_manifest(repository, args.gold)
    print(
        json.dumps(
            {
                "line_count": repository.line_count,
                "source_manifest_sha256": repository.source_manifest_sha256,
                "gold_queries": len(repository.gold_queries),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
