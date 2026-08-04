"""Generate the deterministic public Python navigation qualification corpus.

The generator performs file writes only. Git initialization, provider discovery,
Pyright startup, package installation, and network access belong to the runner.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from pathlib import Path

FIXTURE_SEED = 411
FIXTURE_LINES = 100_000
PYRIGHT_VERSION = "1.1.411"
DEFINITION_QUERIES = 200
REFERENCE_QUERIES = 100
CALL_QUERIES = 100
MUTATION_CYCLES = 50
PADDING_MODULES = 32
PADDING_BLOCK_LINES = 64
FIXTURE_MIN_PYTHON_BYTES = 2 * 1024 * 1024
FIXTURE_MAX_PYTHON_BYTES = 4 * 1024 * 1024

SOURCE_MANIFEST_SCHEMA = "code-navigation-source-manifest/v1"
GOLD_SCHEMA_VERSION = "code-navigation-python-gold/v1"
WORKLOAD_CATALOG_SCHEMA = "code-navigation-mutation-workloads/v1"
WORKLOAD_CATALOG_PATH = "workloads/catalog.py"


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class GoldLocation:
    """One exact normalized location backed by current source bytes."""

    path: str
    line: int
    character: int
    byte_start: int
    byte_end: int
    source_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "character": self.character,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class GoldQuery:
    """One provider query and the complete exact set expected from it."""

    query_id: str
    capability: str
    path: str
    line: int
    character: int
    codepoint_character: int
    byte_offset: int
    byte_end: int
    source_sha256: str
    symbol: str
    direction: str | None
    expected_locations: tuple[GoldLocation, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "query_id": self.query_id,
            "capability": self.capability,
            "path": self.path,
            "line": self.line,
            "character": self.character,
            "codepoint_character": self.codepoint_character,
            "byte_offset": self.byte_offset,
            "byte_end": self.byte_end,
            "source_sha256": self.source_sha256,
            "symbol": self.symbol,
            "direction": self.direction,
            "expected_locations": [location.as_dict() for location in self.expected_locations],
        }


@dataclass(frozen=True, slots=True)
class MutationWorkload:
    """Baseline and replacement bytes for one create/edit/rename/delete cycle."""

    workload_id: str
    original_path: str
    renamed_path: str
    created_path: str
    probe_path: str
    original_content: bytes
    edited_content: bytes
    created_content: bytes
    baseline_probe_content: bytes
    create_probe_content: bytes
    edit_probe_content: bytes
    rename_probe_content: bytes
    rename_old_probe_content: bytes
    delete_probe_content: bytes
    original_symbol: str
    edited_symbol: str
    created_symbol: str


@dataclass(frozen=True, slots=True)
class QualificationRepository:
    root: Path
    line_count: int
    source_manifest_sha256: str
    gold_sha256: str
    source_manifest: tuple[tuple[str, str], ...]
    gold_queries: tuple[GoldQuery, ...]
    ambiguous_symbols: tuple[str, ...]
    feature_inventory: tuple[str, ...]
    workloads: tuple[MutationWorkload, ...]


def workload_digest(workload: MutationWorkload) -> str:
    """Hash every behavior-relevant field in one mutation workload."""
    payload: list[dict[str, object]] = []
    for field in fields(workload):
        value = getattr(workload, field.name)
        encoded: object
        if isinstance(value, bytes):
            encoded = {"encoding": "hex", "value": value.hex()}
        else:
            encoded = value
        payload.append({"name": field.name, "value": encoded})
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "schema_version": WORKLOAD_CATALOG_SCHEMA,
                "fields": payload,
            }
        )
    ).hexdigest()


def workload_catalog_bytes(workloads: tuple[MutationWorkload, ...]) -> bytes:
    """Render the deterministic Python catalog for all mutation variants."""
    lines = [
        '"""Canonical mutation workload identities."""',
        "",
        f"WORKLOAD_CATALOG_SCHEMA = {WORKLOAD_CATALOG_SCHEMA!r}",
        "WORKLOAD_DIGESTS = (",
    ]
    lines.extend(
        f"    ({workload.workload_id!r}, {workload_digest(workload)!r}),"
        for workload in workloads
    )
    lines.append(")")
    return ("\n".join(lines) + "\n").encode("ascii")


def _symbol(index: int) -> str:
    # Every twentieth target exercises non-ASCII identifier byte ranges.
    return f"\u0446\u0435\u043b\u044c_{index:03d}" if index % 20 == 0 else f"target_{index:03d}"


def _definition_sources() -> dict[str, bytes]:
    definitions = [
        '"""Cross-file qualification definitions."""',
        "from __future__ import annotations",
        "",
    ]
    users = [
        '"""Definition and reference query sites."""',
        "from __future__ import annotations",
        "",
    ]
    callers = [
        '"""Outgoing call-hierarchy query sites."""',
        "from __future__ import annotations",
        "",
    ]
    for index in range(DEFINITION_QUERIES):
        symbol = _symbol(index)
        definitions.extend(
            [
                f"def {symbol}(value: int) -> int:",
                f"    return value + {index}",
                "",
            ]
        )
        users.append(f"from qual.definitions import {symbol}")
    users.append("")
    for index in range(DEFINITION_QUERIES):
        symbol = _symbol(index)
        users.extend(
            [
                f"def use_definition_{index:03d}(value: int) -> int:",
                f"    \u043f\u0440\u0435\u0444\u0438\u043a\u0441_{index:03d} = {index}",
                f"    return \u043f\u0440\u0435\u0444\u0438\u043a\u0441_{index:03d} + {symbol}(value)",
                "",
            ]
        )
    for index in range(CALL_QUERIES):
        callers.append(f"from qual.definitions import {_symbol(index)}")
    callers.append("")
    for index in range(CALL_QUERIES):
        symbol = _symbol(index)
        callers.extend(
            [
                f"def caller_{index:03d}(value: int) -> int:",
                f"    return {symbol}(value)",
                "",
            ]
        )
    return {
        "qual/__init__.py": b'"""Qualification package."""\n',
        "qual/definitions.py": ("\n".join(definitions) + "\n").encode("utf-8"),
        "qual/definition_users.py": ("\n".join(users) + "\n").encode("utf-8"),
        "qual/callers.py": ("\n".join(callers) + "\n").encode("utf-8"),
    }


def _semantic_sources() -> dict[str, bytes]:
    semantics = """\
\"\"\"Inheritance and structural Protocol qualification cases.\"\"\"
from __future__ import annotations
from typing import Protocol

class WorkerProtocol(Protocol):
    def run(self, value: int) -> int: ...

class BaseWorker:
    def run(self, value: int) -> int:
        return value

class ConcreteWorker(BaseWorker):
    def run(self, value: int) -> int:
        return super().run(value) + 1

def invoke_protocol(worker: WorkerProtocol, value: int) -> int:
    return worker.run(value)
"""
    ambiguous_a = """\
def execute(value: int) -> int:
    return value + 1
"""
    ambiguous_b = """\
def execute(value: int) -> int:
    return value - 1
"""
    return {
        "qual/semantics.py": semantics.encode("utf-8"),
        "qual/ambiguous_alpha.py": ambiguous_a.encode("utf-8"),
        "qual/ambiguous_beta.py": ambiguous_b.encode("utf-8"),
        "broken/syntax_000.py": b"def intentionally_broken_000(:\n    pass\n",
        "broken/syntax_001.py": b"class IntentionallyBroken001(\n    pass\n",
        "broken/type_002.py": b"value: int = 'intentional type error'\n",
    }


def _workload_sources() -> tuple[dict[str, bytes], tuple[MutationWorkload, ...]]:
    sources: dict[str, bytes] = {"workloads/__init__.py": b""}
    workloads: list[MutationWorkload] = []
    original_path = "workloads/mutable_target.py"
    renamed_path = "workloads/renamed_target.py"
    created_path = "workloads/created_target.py"
    probe_path = "workloads/probe.py"
    for index in range(MUTATION_CYCLES):
        original_symbol = f"workload_original_{index:03d}"
        edited_symbol = f"workload_edited_{index:03d}"
        created_symbol = f"workload_created_{index:03d}"
        original = (
            f'def {original_symbol}() -> str:\n    return "initial-{index:03d}"\n\n'
        ).encode()
        edited = (
            f'"""Edited workload {index:03d}."""\n\n'
            f"def {edited_symbol}() -> str:\n"
            f'    return "edited-{index:03d}"\n\n'
        ).encode()
        created = (f'def {created_symbol}() -> str:\n    return "created-{index:03d}"\n\n').encode()
        def probe_content(module: str, symbol: str, phase: str) -> bytes:
            return (
                f"from workloads.{module} import {symbol}\n\n"
                f"def {phase}_probe_{index:03d}() -> str:\n"
                f"    return {symbol}()\n"
            ).encode()

        baseline_probe = (
            f'def baseline_probe_{index:03d}() -> str:\n    return "baseline-{index:03d}"\n'
        ).encode()
        create_probe = probe_content("created_target", created_symbol, "create")
        edit_probe = probe_content("mutable_target", edited_symbol, "edit")
        rename_probe = probe_content("renamed_target", edited_symbol, "rename_new")
        rename_old_probe = probe_content("mutable_target", edited_symbol, "rename_old")
        delete_probe = probe_content("renamed_target", edited_symbol, "delete")
        workloads.append(
            MutationWorkload(
                workload_id=f"mutation-{index:03d}",
                original_path=original_path,
                renamed_path=renamed_path,
                created_path=created_path,
                probe_path=probe_path,
                original_content=original,
                edited_content=edited,
                created_content=created,
                baseline_probe_content=baseline_probe,
                create_probe_content=create_probe,
                edit_probe_content=edit_probe,
                rename_probe_content=rename_probe,
                rename_old_probe_content=rename_old_probe,
                delete_probe_content=delete_probe,
                original_symbol=original_symbol,
                edited_symbol=edited_symbol,
                created_symbol=created_symbol,
            )
        )
    result = tuple(workloads)
    sources[original_path] = result[0].original_content
    sources[probe_path] = result[0].baseline_probe_content
    sources[WORKLOAD_CATALOG_PATH] = workload_catalog_bytes(result)
    return sources, result


def _line_count(sources: dict[str, bytes]) -> int:
    return sum(content.count(b"\n") for content in sources.values())


def _with_padding(sources: dict[str, bytes]) -> tuple[str, ...]:
    fixed_user_lines = 3 + PADDING_MODULES + 1 + 1 + PADDING_MODULES + 1
    remaining = FIXTURE_LINES - _line_count(sources) - 1 - fixed_user_lines
    if remaining < PADDING_MODULES * PADDING_BLOCK_LINES:
        raise RuntimeError("semantic qualification sources exceed the line budget")
    sources["padding/__init__.py"] = b'"""Executable qualification padding."""\n'
    user_operation_lines = remaining % PADDING_BLOCK_LINES
    total_blocks = (remaining - user_operation_lines) // PADDING_BLOCK_LINES
    module_blocks, extra_blocks = divmod(total_blocks, PADDING_MODULES)
    operation_index = 0
    entry_names: list[str] = []
    for module_index in range(PADDING_MODULES):
        block_count = module_blocks + int(module_index < extra_blocks)
        lines: list[str] = []
        for block_index in range(block_count):
            function_name = f"padding_{module_index:02d}_{block_index:03d}"
            lines.append(f"def {function_name}(accumulator: int) -> int:")
            if block_index:
                previous_name = f"padding_{module_index:02d}_{block_index - 1:03d}"
                lines.append(f"    accumulator = {previous_name}(accumulator)")
            operation_lines = PADDING_BLOCK_LINES - 2 - int(block_index > 0)
            for _index in range(operation_lines):
                value = FIXTURE_SEED * 10_000_000 + operation_index
                lines.append(f"    accumulator += {value}")
                operation_index += 1
            lines.append("    return accumulator")
        entry_names.append(f"padding_{module_index:02d}_{block_count - 1:03d}")
        sources[f"padding/module_{module_index:02d}.py"] = (
            "\n".join(lines) + "\n"
        ).encode("ascii")

    user_lines = [
        '"""Statically reachable qualification padding entry points."""',
        "from __future__ import annotations",
        "",
    ]
    user_lines.extend(
        f"from padding.module_{module_index:02d} import {entry_name}"
        for module_index, entry_name in enumerate(entry_names)
    )
    user_lines.extend(("", "def exercise_padding(accumulator: int) -> int:"))
    user_lines.extend(
        f"    accumulator = {entry_name}(accumulator)" for entry_name in entry_names
    )
    for _index in range(user_operation_lines):
        value = FIXTURE_SEED * 10_000_000 + operation_index
        user_lines.append(f"    accumulator += {value}")
        operation_index += 1
    user_lines.append("    return accumulator")
    sources["qual/padding_users.py"] = ("\n".join(user_lines) + "\n").encode("ascii")

    if _line_count(sources) != FIXTURE_LINES:
        raise AssertionError("qualification line padding is not exact")
    source_bytes = sum(map(len, sources.values()))
    if source_bytes < FIXTURE_MIN_PYTHON_BYTES:
        raise AssertionError("qualification Python sources are below the byte floor")
    if source_bytes > FIXTURE_MAX_PYTHON_BYTES:
        raise AssertionError("qualification Python sources exceed the byte budget")
    return tuple(entry_names)


def _source_manifest_from_root(root: Path) -> tuple[tuple[str, str], ...]:
    python_sources = (
        path
        for path in root.rglob("*.py")
        if ".git" not in path.relative_to(root).parts
    )
    paths = sorted((*python_sources, root / "pyrightconfig.json"))
    return tuple(
        (
            path.relative_to(root).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in paths
    )


def _source_manifest_sha256(manifest: tuple[tuple[str, str], ...]) -> str:
    payload = {
        "schema_version": SOURCE_MANIFEST_SCHEMA,
        "files": [{"path": path, "sha256": digest} for path, digest in manifest],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def current_source_manifest_sha256(root: Path) -> str:
    """Hash generated Python files and Pyright configuration by path and digest."""
    resolved = Path(root).resolve(strict=True)
    return _source_manifest_sha256(_source_manifest_from_root(resolved))


def _starts(content: bytes, symbol: str) -> tuple[int, ...]:
    needle = symbol.encode("utf-8")
    starts: list[int] = []
    offset = 0
    while True:
        found = content.find(needle, offset)
        if found < 0:
            return tuple(starts)
        starts.append(found)
        offset = found + len(needle)


def _position(content: bytes, byte_start: int) -> tuple[int, int, int]:
    line_start = content.rfind(b"\n", 0, byte_start) + 1
    prefix = content[line_start:byte_start]
    return (
        content.count(b"\n", 0, byte_start) + 1,
        len(prefix),
        len(prefix.decode("utf-8", errors="strict")),
    )


def _location(
    sources: dict[str, bytes],
    digests: dict[str, str],
    path: str,
    symbol: str,
    occurrence: int,
) -> GoldLocation:
    content = sources[path]
    byte_start = _starts(content, symbol)[occurrence]
    line, character, _codepoint_character = _position(content, byte_start)
    return GoldLocation(
        path,
        line,
        character,
        byte_start,
        byte_start + len(symbol.encode("utf-8")),
        digests[path],
    )


def _query(
    *,
    query_id: str,
    capability: str,
    path: str,
    symbol: str,
    occurrence: int,
    expected: tuple[GoldLocation, ...],
    sources: dict[str, bytes],
    digests: dict[str, str],
    direction: str | None = None,
) -> GoldQuery:
    content = sources[path]
    byte_start = _starts(content, symbol)[occurrence]
    line, character, codepoint_character = _position(content, byte_start)
    return GoldQuery(
        query_id,
        capability,
        path,
        line,
        character,
        codepoint_character,
        byte_start,
        byte_start + len(symbol.encode("utf-8")),
        digests[path],
        symbol,
        direction,
        expected,
    )


def _gold_queries(
    sources: dict[str, bytes],
    source_manifest: tuple[tuple[str, str], ...],
    padding_entries: tuple[str, ...],
) -> tuple[GoldQuery, ...]:
    digests = dict(source_manifest)
    queries: list[GoldQuery] = []
    for index in range(DEFINITION_QUERIES):
        if index >= DEFINITION_QUERIES - PADDING_MODULES:
            module_index = index - (DEFINITION_QUERIES - PADDING_MODULES)
            symbol = padding_entries[module_index]
            target_path = f"padding/module_{module_index:02d}.py"
            query_path = "qual/padding_users.py"
        else:
            symbol = _symbol(index)
            target_path = "qual/definitions.py"
            query_path = "qual/definition_users.py"
        target = _location(sources, digests, target_path, symbol, 0)
        queries.append(
            _query(
                query_id=f"definition-{index:03d}",
                capability="definition",
                path=query_path,
                symbol=symbol,
                occurrence=1,
                expected=(target,),
                sources=sources,
                digests=digests,
            )
        )
    for index in range(REFERENCE_QUERIES):
        symbol = _symbol(index)
        expected = (
            _location(sources, digests, "qual/definitions.py", symbol, 0),
            _location(sources, digests, "qual/definition_users.py", symbol, 0),
            _location(sources, digests, "qual/definition_users.py", symbol, 1),
            _location(sources, digests, "qual/callers.py", symbol, 0),
            _location(sources, digests, "qual/callers.py", symbol, 1),
        )
        queries.append(
            _query(
                query_id=f"references-{index:03d}",
                capability="references",
                path="qual/definitions.py",
                symbol=symbol,
                occurrence=0,
                expected=tuple(sorted(expected, key=lambda item: (item.path, item.byte_start))),
                sources=sources,
                digests=digests,
            )
        )
    for index in range(CALL_QUERIES):
        symbol = _symbol(index)
        target = _location(sources, digests, "qual/definitions.py", symbol, 0)
        queries.append(
            _query(
                query_id=f"calls-{index:03d}",
                capability="calls",
                path="qual/callers.py",
                symbol=f"caller_{index:03d}",
                occurrence=0,
                expected=(target,),
                sources=sources,
                digests=digests,
                direction="outgoing",
            )
        )
    return tuple(queries)


def _gold_payload(
    *,
    line_count: int,
    source_manifest_sha256: str,
    queries: tuple[GoldQuery, ...],
) -> dict[str, object]:
    return {
        "schema_version": GOLD_SCHEMA_VERSION,
        "fixture_seed": FIXTURE_SEED,
        "fixture_lines": line_count,
        "source_manifest_sha256": source_manifest_sha256,
        "definition_queries": DEFINITION_QUERIES,
        "reference_queries": REFERENCE_QUERIES,
        "call_queries": CALL_QUERIES,
        "queries": [query.as_dict() for query in queries],
    }


def gold_payload(repository: QualificationRepository) -> dict[str, object]:
    """Return the canonical closed gold domain for ``repository``."""
    return _gold_payload(
        line_count=repository.line_count,
        source_manifest_sha256=repository.source_manifest_sha256,
        queries=repository.gold_queries,
    )


def current_gold_sha256(repository: QualificationRepository) -> str:
    """Hash the current in-memory canonical gold domain."""
    return hashlib.sha256(_canonical_json_bytes(gold_payload(repository))).hexdigest()


def generate_qualification_repository(destination: Path) -> QualificationRepository:
    """Write an exact, byte-stable 100 KLOC repository under ``destination``."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    sources = _definition_sources()
    sources.update(_semantic_sources())
    workload_sources, workloads = _workload_sources()
    sources.update(workload_sources)
    padding_entries = _with_padding(sources)

    for relative, content in sorted(sources.items()):
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (destination / "pyrightconfig.json").write_bytes(
        _canonical_json_bytes(
            {
                "include": ["qual", "workloads", "broken", "padding"],
                "pythonVersion": "3.10",
                "typeCheckingMode": "off",
            }
        )
        + b"\n"
    )

    source_manifest = _source_manifest_from_root(destination)
    source_hash = _source_manifest_sha256(source_manifest)
    queries = _gold_queries(sources, source_manifest, padding_entries)
    payload = _gold_payload(
        line_count=_line_count(sources),
        source_manifest_sha256=source_hash,
        queries=queries,
    )
    gold_hash = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    return QualificationRepository(
        destination,
        _line_count(sources),
        source_hash,
        gold_hash,
        source_manifest,
        queries,
        ("execute",),
        (
            "ambiguous_names",
            "broken_files",
            "cross_file_calls",
            "cross_file_definitions",
            "cross_file_imports",
            "cross_file_references",
            "inheritance",
            "protocols",
            "unicode_identifiers",
        ),
        workloads,
    )


def write_gold_manifest(repository: QualificationRepository, path: Path) -> None:
    path.write_bytes(_canonical_json_bytes(gold_payload(repository)) + b"\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate the Python qualification corpus")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--gold", type=Path, default=None)
    args = parser.parse_args(argv)
    repository = generate_qualification_repository(args.destination)
    if args.gold is not None:
        write_gold_manifest(repository, args.gold)
    print(
        json.dumps(
            {
                "line_count": repository.line_count,
                "source_manifest_sha256": repository.source_manifest_sha256,
                "gold_sha256": repository.gold_sha256,
                "gold_queries": len(repository.gold_queries),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
