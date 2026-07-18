"""Immutable Evidence Graph generation storage and bounded read queries."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath

from reliable_memory import canonical_json_bytes, validate_runtime_file

GRAPH_SCHEMA_VERSION = "evidence-graph/v1"
MAX_DATABASE_BYTES = 16 * 1024 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024 * 1024
MAX_ROWS = 10_000
MAX_DEPTH = 32
PROGRESS_OPCODES = 1000

_SHA256 = frozenset("0123456789abcdef")
_CONFIDENCE = frozenset({"high", "medium", "low"})
_AUTHORITY = frozenset({"user", "web", "ai-derived", "inferred"})
_RESOLUTION = frozenset({"resolved", "unresolved", "ambiguous"})
_OBSERVATION_REASONS = frozenset(
    {
        "ambiguous_target",
        "dynamic_dispatch",
        "missing_dependency",
        "parse_error",
        "unresolved_reference",
        "unsupported_semantics",
    }
)

_SOURCE_KEYS = frozenset(
    {"source_id", "relative_path", "sha256", "size", "media_type", "language", "git_oid"}
)
_NODE_KEYS = frozenset(
    {"node_id", "kind", "identity_scheme", "identity_key", "metadata"}
)
_OCCURRENCE_KEYS = frozenset(
    {
        "occurrence_id",
        "node_id",
        "source_id",
        "role",
        "byte_start",
        "byte_end",
        "line_start",
        "line_end",
    }
)
_ASSERTION_KEYS = frozenset(
    {
        "assertion_id",
        "source_node_id",
        "edge_type",
        "target_node_id",
        "literal",
        "confidence",
        "authority",
        "resolution",
        "extractor",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "evidence_id",
        "assertion_id",
        "observation_id",
        "source_id",
        "byte_start",
        "byte_end",
        "span_sha256",
    }
)
_OBSERVATION_KEYS = frozenset(
    {"observation_id", "source_node_id", "edge_type", "target_text", "reason", "extractor"}
)
_DEPENDENCY_KEYS = frozenset(
    {"dependency_id", "dependent_node_id", "dependency_node_id", "kind", "source_id"}
)

_SCHEMA = """
CREATE TABLE source (
  source_id TEXT PRIMARY KEY,
  relative_path TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL CHECK (size >= 0),
  media_type TEXT NOT NULL,
  language TEXT,
  git_oid TEXT
) WITHOUT ROWID;
CREATE TABLE node (
  node_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  identity_scheme TEXT NOT NULL,
  identity_key TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(identity_scheme, identity_key)
) WITHOUT ROWID;
CREATE TABLE occurrence (
  occurrence_id TEXT PRIMARY KEY,
  node_id TEXT REFERENCES node(node_id),
  source_id TEXT NOT NULL REFERENCES source(source_id),
  role TEXT NOT NULL,
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end >= byte_start),
  line_start INTEGER NOT NULL CHECK (line_start >= 1),
  line_end INTEGER NOT NULL CHECK (line_end >= line_start)
) WITHOUT ROWID;
CREATE TABLE assertion (
  assertion_id TEXT PRIMARY KEY,
  source_node_id TEXT NOT NULL REFERENCES node(node_id),
  edge_type TEXT NOT NULL,
  target_node_id TEXT REFERENCES node(node_id),
  literal_json TEXT,
  confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
  authority TEXT NOT NULL CHECK (authority IN ('user', 'web', 'ai-derived', 'inferred')),
  resolution TEXT NOT NULL CHECK (resolution IN ('resolved', 'unresolved', 'ambiguous')),
  extractor TEXT NOT NULL,
  CHECK ((target_node_id IS NULL) != (literal_json IS NULL))
) WITHOUT ROWID;
CREATE TABLE observation (
  observation_id TEXT PRIMARY KEY,
  source_node_id TEXT REFERENCES node(node_id),
  edge_type TEXT NOT NULL,
  target_text TEXT,
  reason TEXT NOT NULL CHECK (reason IN ('ambiguous_target', 'dynamic_dispatch',
    'missing_dependency', 'parse_error', 'unresolved_reference', 'unsupported_semantics')),
  extractor TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  assertion_id TEXT REFERENCES assertion(assertion_id),
  observation_id TEXT REFERENCES observation(observation_id),
  source_id TEXT NOT NULL REFERENCES source(source_id),
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end >= byte_start),
  span_sha256 TEXT NOT NULL,
  CHECK ((assertion_id IS NULL) != (observation_id IS NULL))
) WITHOUT ROWID;
CREATE TABLE dependency (
  dependency_id TEXT PRIMARY KEY,
  dependent_node_id TEXT NOT NULL REFERENCES node(node_id),
  dependency_node_id TEXT NOT NULL REFERENCES node(node_id),
  kind TEXT NOT NULL,
  source_id TEXT REFERENCES source(source_id)
) WITHOUT ROWID;
CREATE INDEX node_kind ON node(kind, identity_key, node_id);
CREATE INDEX occurrence_source_span ON occurrence(source_id, byte_start, byte_end, occurrence_id);
CREATE INDEX assertion_traversal ON assertion(source_node_id, edge_type, target_node_id, assertion_id);
CREATE INDEX assertion_reverse ON assertion(target_node_id, edge_type, source_node_id, assertion_id);
CREATE INDEX assertion_resolution ON assertion(resolution, edge_type, assertion_id);
CREATE INDEX evidence_source_span ON evidence(source_id, byte_start, byte_end, evidence_id);
CREATE INDEX observation_resolution ON observation(reason, edge_type, observation_id);
CREATE INDEX dependency_invalidation ON dependency(dependency_node_id, kind, dependent_node_id, dependency_id);
CREATE INDEX dependency_reverse ON dependency(dependent_node_id, kind, dependency_node_id, dependency_id);
"""


def _closed(record: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if not isinstance(record, Mapping):
        raise TypeError(f"{label} must be an object")
    if set(record) != expected:
        raise ValueError(f"{label} must be a closed object with no missing or unknown fields")


def _text(value: object, label: str, *, maximum: int = 4096, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(character in value for character in "\x00\r\n")
    ):
        raise ValueError(f"{label} must be a bounded non-empty string")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _SHA256 for c in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hash")
    return value


def _integer(value: object, label: str, *, minimum: int = 0, maximum: int = MAX_SOURCE_BYTES) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{label} is outside its supported range")
    return value


def _relative_path(value: object) -> str:
    text = _text(value, "relative_path")
    assert text is not None
    if "\\" in text:
        raise ValueError("relative_path must use normalized POSIX separators")
    path = PurePosixPath(text)
    if path.is_absolute() or text != path.as_posix() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("relative_path must remain inside the captured source root")
    return text


def _canonical_json(value: object, label: str) -> str:
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite canonical JSON") from exc
    if len(encoded) > 1024 * 1024:
        raise ValueError(f"{label} exceeds the supported bound")
    return encoded.decode("utf-8")


def _ordered(records: Iterable[Mapping[str, object]], key: str, label: str) -> list[Mapping[str, object]]:
    values = list(records)
    if len(values) > 1_000_000:
        raise ValueError(f"{label} row ceiling exceeded")
    return sorted(values, key=lambda record: str(record.get(key, "")))


def _configure_write(database: sqlite3.Connection) -> None:
    mode = database.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    if str(mode).casefold() != "delete":
        raise sqlite3.OperationalError("Evidence Graph requires rollback-journal DELETE mode")
    database.execute("PRAGMA synchronous=FULL")
    database.execute("PRAGMA foreign_keys=ON")
    database.execute("PRAGMA trusted_schema=OFF")


def create_generation_database(
    database_path: Path,
    *,
    sources: Iterable[Mapping[str, object]],
    source_bytes: Mapping[str, bytes],
    nodes: Iterable[Mapping[str, object]],
    occurrences: Iterable[Mapping[str, object]],
    assertions: Iterable[Mapping[str, object]],
    evidence: Iterable[Mapping[str, object]],
    observations: Iterable[Mapping[str, object]],
    dependencies: Iterable[Mapping[str, object]],
) -> None:
    """Create one complete graph database; existing artifacts are never replaced."""
    path = Path(database_path)
    if path.exists() or path.is_symlink():
        raise FileExistsError("Evidence Graph generation artifacts are immutable")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    metadata = path.parent.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & reparse:
        raise PermissionError("Evidence Graph generation directory must not be a link or reparse point")
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    source_content: dict[str, bytes] = {}
    try:
        source_rows = _ordered(sources, "source_id", "source")
        if set(source_bytes) != {record.get("source_id") for record in source_rows}:
            raise ValueError("source_bytes must bind every captured source exactly once")
        normalized_sources = []
        for record in source_rows:
            _closed(record, _SOURCE_KEYS, "source")
            source_id = _text(record["source_id"], "source_id", maximum=512)
            assert source_id is not None
            content = source_bytes[source_id]
            if not isinstance(content, bytes) or len(content) > MAX_SOURCE_BYTES:
                raise TypeError("captured source content must be bounded bytes")
            size = _integer(record["size"], "source size")
            digest = _digest(record["sha256"], "source hash")
            if size != len(content) or digest != hashlib.sha256(content).hexdigest():
                raise ValueError("captured source size or hash does not match source bytes")
            source_content[source_id] = content
            normalized_sources.append(
                (
                    source_id,
                    _relative_path(record["relative_path"]),
                    digest,
                    size,
                    _text(record["media_type"], "media_type", maximum=256),
                    _text(record["language"], "language", maximum=128, optional=True),
                    _text(record["git_oid"], "git_oid", maximum=128, optional=True),
                )
            )

        normalized_nodes = []
        for record in _ordered(nodes, "node_id", "node"):
            _closed(record, _NODE_KEYS, "node")
            normalized_nodes.append(
                (
                    _text(record["node_id"], "node_id", maximum=512),
                    _text(record["kind"], "node kind", maximum=128),
                    _text(record["identity_scheme"], "identity_scheme", maximum=256),
                    _text(record["identity_key"], "identity_key", maximum=4096),
                    _canonical_json(record["metadata"], "node metadata"),
                )
            )

        normalized_occurrences = []
        for record in _ordered(occurrences, "occurrence_id", "occurrence"):
            _closed(record, _OCCURRENCE_KEYS, "occurrence")
            source_id = _text(record["source_id"], "source_id", maximum=512)
            assert source_id is not None
            start = _integer(record["byte_start"], "occurrence byte_start")
            end = _integer(record["byte_end"], "occurrence byte_end", minimum=start)
            if source_id not in source_content or end > len(source_content[source_id]):
                raise ValueError("occurrence byte range is outside its captured source")
            normalized_occurrences.append(
                (
                    _text(record["occurrence_id"], "occurrence_id", maximum=512),
                    _text(record["node_id"], "node_id", maximum=512, optional=True),
                    source_id,
                    _text(record["role"], "occurrence role", maximum=128),
                    start,
                    end,
                    _integer(record["line_start"], "line_start", minimum=1, maximum=2**31 - 1),
                    _integer(record["line_end"], "line_end", minimum=1, maximum=2**31 - 1),
                )
            )

        normalized_assertions = []
        for record in _ordered(assertions, "assertion_id", "assertion"):
            _closed(record, _ASSERTION_KEYS, "assertion")
            target = _text(record["target_node_id"], "target_node_id", maximum=512, optional=True)
            literal = None if record["literal"] is None else _canonical_json(record["literal"], "literal")
            if (target is None) == (literal is None):
                raise ValueError("assertion must have exactly one target node or literal")
            confidence = record["confidence"]
            authority = record["authority"]
            resolution = record["resolution"]
            if confidence not in _CONFIDENCE or authority not in _AUTHORITY:
                raise ValueError("assertion confidence or authority is outside the closed contract")
            if resolution not in _RESOLUTION or (resolution == "resolved" and target is None):
                raise ValueError("assertion resolution is outside the closed contract")
            normalized_assertions.append(
                (
                    _text(record["assertion_id"], "assertion_id", maximum=512),
                    _text(record["source_node_id"], "source_node_id", maximum=512),
                    _text(record["edge_type"], "edge_type", maximum=128),
                    target,
                    literal,
                    confidence,
                    authority,
                    resolution,
                    _text(record["extractor"], "extractor", maximum=256),
                )
            )

        normalized_observations = []
        for record in _ordered(observations, "observation_id", "observation"):
            _closed(record, _OBSERVATION_KEYS, "observation")
            if record["reason"] not in _OBSERVATION_REASONS:
                raise ValueError("observation reason is outside the controlled reason set")
            normalized_observations.append(
                (
                    _text(record["observation_id"], "observation_id", maximum=512),
                    _text(record["source_node_id"], "source_node_id", maximum=512, optional=True),
                    _text(record["edge_type"], "edge_type", maximum=128),
                    _text(record["target_text"], "target_text", optional=True),
                    record["reason"],
                    _text(record["extractor"], "extractor", maximum=256),
                )
            )

        normalized_evidence = []
        for record in _ordered(evidence, "evidence_id", "evidence"):
            _closed(record, _EVIDENCE_KEYS, "evidence")
            assertion_id = _text(record["assertion_id"], "assertion_id", maximum=512, optional=True)
            observation_id = _text(
                record["observation_id"], "observation_id", maximum=512, optional=True
            )
            if (assertion_id is None) == (observation_id is None):
                raise ValueError("evidence must bind exactly one assertion or observation")
            source_id = _text(record["source_id"], "source_id", maximum=512)
            assert source_id is not None
            start = _integer(record["byte_start"], "evidence byte_start")
            end = _integer(record["byte_end"], "evidence byte_end", minimum=start)
            if source_id not in source_content or end > len(source_content[source_id]):
                raise ValueError("evidence byte range is outside its captured source")
            span_hash = _digest(record["span_sha256"], "evidence span hash")
            if hashlib.sha256(source_content[source_id][start:end]).hexdigest() != span_hash:
                raise ValueError("evidence span hash does not match the captured source range")
            normalized_evidence.append(
                (
                    _text(record["evidence_id"], "evidence_id", maximum=512),
                    assertion_id,
                    observation_id,
                    source_id,
                    start,
                    end,
                    span_hash,
                )
            )

        normalized_dependencies = []
        for record in _ordered(dependencies, "dependency_id", "dependency"):
            _closed(record, _DEPENDENCY_KEYS, "dependency")
            normalized_dependencies.append(
                (
                    _text(record["dependency_id"], "dependency_id", maximum=512),
                    _text(record["dependent_node_id"], "dependent_node_id", maximum=512),
                    _text(record["dependency_node_id"], "dependency_node_id", maximum=512),
                    _text(record["kind"], "dependency kind", maximum=128),
                    _text(record["source_id"], "source_id", maximum=512, optional=True),
                )
            )

        database = sqlite3.connect(temporary)
        try:
            _configure_write(database)
            database.executescript(_SCHEMA)
            database.executemany("INSERT INTO source VALUES (?, ?, ?, ?, ?, ?, ?)", normalized_sources)
            database.executemany("INSERT INTO node VALUES (?, ?, ?, ?, ?)", normalized_nodes)
            database.executemany("INSERT INTO occurrence VALUES (?, ?, ?, ?, ?, ?, ?, ?)", normalized_occurrences)
            database.executemany("INSERT INTO assertion VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", normalized_assertions)
            database.executemany("INSERT INTO observation VALUES (?, ?, ?, ?, ?, ?)", normalized_observations)
            database.executemany("INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)", normalized_evidence)
            database.executemany("INSERT INTO dependency VALUES (?, ?, ?, ?, ?)", normalized_dependencies)
            violations = database.execute("PRAGMA foreign_key_check").fetchone()
            if violations is not None:
                raise ValueError("Evidence Graph records violate referential integrity")
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()
        if temporary.stat().st_size > MAX_DATABASE_BYTES:
            raise ValueError("Evidence Graph database exceeds the supported byte ceiling")
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _bound(value: object, label: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be between 1 and {maximum}")
    return value


class EvidenceGraph:
    """Read-only facade over one catalog-selected immutable graph generation."""

    def __init__(
        self,
        database_path: Path,
        *,
        state_root: Path,
        generation_id: str | None = None,
        max_database_bytes: int = MAX_DATABASE_BYTES,
    ) -> None:
        self.database_path = Path(database_path)
        self.state_root = Path(state_root)
        self.generation_id = generation_id
        try:
            expected = validate_runtime_file(
                self.database_path, self.state_root, max_bytes=max_database_bytes
            )
        except FileNotFoundError as exc:
            raise PermissionError("Evidence Graph must remain inside an existing state root") from exc
        uri = f"{self.database_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        database = sqlite3.connect(uri, uri=True, timeout=0)
        try:
            current = self.database_path.stat(follow_symlinks=False)
            if not os.path.samestat(expected, current):
                raise PermissionError("Evidence Graph identity changed while opening")
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA query_only=ON")
            database.execute("PRAGMA trusted_schema=OFF")
            self._database = database
        except BaseException:
            database.close()
            raise

    @classmethod
    def open_active(cls, catalog: object, *, deadline: float | None = None) -> EvidenceGraph | None:
        options = {} if deadline is None else {"deadline": deadline}
        manifest = catalog.get_active(**options)
        if manifest is None:
            return None
        if manifest.get("graph_schema_version") != GRAPH_SCHEMA_VERSION:
            raise ValueError("active generation does not use the Evidence Graph schema")
        generation_id = manifest["generation_id"]
        artifacts = {item["path"] for item in manifest["artifacts"]}
        if "evidence.sqlite3" not in artifacts:
            raise ValueError("active graph generation has no evidence.sqlite3 artifact")
        return cls(
            catalog.generations_path / generation_id / "evidence.sqlite3",
            state_root=catalog.state_root,
            generation_id=generation_id,
        )

    def close(self) -> None:
        self._database.close()

    def __enter__(self) -> EvidenceGraph:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _execute(
        self,
        sql: str,
        parameters: Sequence[object],
        *,
        max_rows: int,
        deadline: float | None,
    ) -> list[sqlite3.Row]:
        limit = _bound(max_rows, "max_rows", MAX_ROWS)
        if deadline is not None and (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise ValueError("deadline must be a finite monotonic timestamp")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Evidence Graph query deadline reached")
        self._database.set_progress_handler(
            None if deadline is None else lambda: int(time.monotonic() >= deadline),
            PROGRESS_OPCODES,
        )
        try:
            rows = self._database.execute(sql, (*parameters, limit + 1)).fetchall()
        except sqlite3.OperationalError as exc:
            if deadline is not None and (time.monotonic() >= deadline or "interrupt" in str(exc).lower()):
                raise TimeoutError("Evidence Graph query deadline reached") from exc
            raise
        finally:
            self._database.set_progress_handler(None, 0)
        if len(rows) > limit:
            raise ValueError("Evidence Graph query row ceiling exceeded")
        return rows

    @staticmethod
    def _node(row: sqlite3.Row) -> dict[str, object]:
        return {
            "node_id": row["node_id"],
            "kind": row["kind"],
            "identity_scheme": row["identity_scheme"],
            "identity_key": row["identity_key"],
            "metadata": json.loads(row["metadata_json"]),
        }

    def node(self, node_id: str) -> dict[str, object] | None:
        identifier = _text(node_id, "node_id", maximum=512)
        row = self._database.execute(
            "SELECT node_id, kind, identity_scheme, identity_key, metadata_json "
            "FROM node WHERE node_id = ?",
            (identifier,),
        ).fetchone()
        return None if row is None else self._node(row)

    def occurrences(self, node_id: str, *, max_rows: int = 100, deadline: float | None = None):
        rows = self._execute(
            "SELECT o.*, s.relative_path FROM occurrence o JOIN source s USING(source_id) "
            "WHERE o.node_id = ? ORDER BY s.relative_path, o.byte_start, o.occurrence_id LIMIT ?",
            (_text(node_id, "node_id", maximum=512),),
            max_rows=max_rows,
            deadline=deadline,
        )
        return [dict(row) for row in rows]

    def _traverse(
        self,
        node_id: str,
        *,
        direction: str,
        edge_types: Sequence[str] | None,
        max_depth: int,
        max_rows: int,
        deadline: float | None,
    ) -> list[dict[str, object]]:
        if direction not in {"in", "out"}:
            raise ValueError("direction must be in or out")
        depth = _bound(max_depth, "max_depth", MAX_DEPTH)
        edge_values = tuple(sorted({_text(value, "edge_type", maximum=128) for value in edge_types or ()}))
        filter_sql = ""
        parameters: list[object] = [_text(node_id, "node_id", maximum=512), depth]
        if edge_values:
            filter_sql = f" AND a.edge_type IN ({','.join('?' for _ in edge_values)})"
            parameters.extend(edge_values)
        source_column, target_column = (
            ("source_node_id", "target_node_id")
            if direction == "out"
            else ("target_node_id", "source_node_id")
        )
        sql = f"""
WITH RECURSIVE walk(node_id, depth, seen) AS (
  SELECT ?, 0, ',' || ? || ','
  UNION ALL
  SELECT a.{target_column}, walk.depth + 1, walk.seen || a.{target_column} || ','
  FROM walk JOIN assertion a ON a.{source_column} = walk.node_id
  WHERE walk.depth < ? AND a.resolution = 'resolved' AND a.target_node_id IS NOT NULL
    AND instr(walk.seen, ',' || a.{target_column} || ',') = 0{filter_sql}
  LIMIT ?
)
SELECT n.node_id, n.kind, n.identity_scheme, n.identity_key, n.metadata_json, min(w.depth) AS depth
FROM walk w JOIN node n USING(node_id) WHERE w.depth > 0
GROUP BY n.node_id ORDER BY depth, n.kind, n.identity_key, n.node_id LIMIT ?
"""
        parameters.insert(1, parameters[0])
        parameters.append(_bound(max_rows, "max_rows", MAX_ROWS) + 1)
        rows = self._execute(sql, parameters, max_rows=max_rows, deadline=deadline)
        result = []
        for row in rows:
            item = self._node(row)
            item["depth"] = row["depth"]
            result.append(item)
        return result

    def neighbors(
        self,
        node_id: str,
        *,
        direction: str = "out",
        edge_types: Sequence[str] | None = None,
        max_depth: int = 1,
        max_rows: int = 100,
        deadline: float | None = None,
    ):
        return self._traverse(
            node_id,
            direction=direction,
            edge_types=edge_types,
            max_depth=max_depth,
            max_rows=max_rows,
            deadline=deadline,
        )

    def callers(self, node_id: str, **options: object):
        return self.neighbors(node_id, direction="in", edge_types=("CALLS",), **options)

    def callees(self, node_id: str, **options: object):
        return self.neighbors(node_id, direction="out", edge_types=("CALLS",), **options)

    def dependencies(
        self,
        node_id: str,
        *,
        reverse: bool = False,
        max_depth: int = 8,
        max_rows: int = 100,
        deadline: float | None = None,
    ):
        if not isinstance(reverse, bool):
            raise ValueError("reverse must be boolean")
        depth = _bound(max_depth, "max_depth", MAX_DEPTH)
        start, target = (
            ("dependency_node_id", "dependent_node_id")
            if reverse
            else ("dependent_node_id", "dependency_node_id")
        )
        rows = self._execute(
            f"""
WITH RECURSIVE walk(node_id, depth, seen) AS (
  SELECT ?, 0, ',' || ? || ','
  UNION ALL
  SELECT d.{target}, w.depth + 1, w.seen || d.{target} || ','
  FROM walk w JOIN dependency d ON d.{start}=w.node_id
  WHERE w.depth < ? AND instr(w.seen, ',' || d.{target} || ',') = 0
  LIMIT ?
)
SELECT n.node_id, n.kind, n.identity_scheme, n.identity_key, n.metadata_json,
       min(w.depth) AS depth
FROM walk w JOIN node n USING(node_id) WHERE w.depth > 0
GROUP BY n.node_id ORDER BY depth, n.kind, n.identity_key, n.node_id LIMIT ?
""",
            (
                _text(node_id, "node_id", maximum=512),
                _text(node_id, "node_id", maximum=512),
                depth,
                _bound(max_rows, "max_rows", MAX_ROWS) + 1,
            ),
            max_rows=max_rows,
            deadline=deadline,
        )
        result = []
        for row in rows:
            item = self._node(row)
            item["depth"] = row["depth"]
            result.append(item)
        return result

    def code_to_doc(self, node_id: str, **options: object):
        return self.neighbors(node_id, direction="in", edge_types=("DOCUMENTS",), **options)

    def doc_to_code(self, node_id: str, **options: object):
        return self.neighbors(node_id, direction="out", edge_types=("DOCUMENTS",), **options)

    def path(
        self,
        source_node_id: str,
        target_node_id: str,
        *,
        max_depth: int = 8,
        max_rows: int = 10,
        deadline: float | None = None,
    ):
        depth = _bound(max_depth, "max_depth", MAX_DEPTH)
        source_id = _text(source_node_id, "source_node_id", maximum=512)
        target_id = _text(target_node_id, "target_node_id", maximum=512)
        rows = self._execute(
            """
WITH RECURSIVE walk(node_id, depth, node_ids, assertion_ids, seen) AS (
  SELECT ?, 0, json_array(?), json_array(), ',' || ? || ','
  UNION ALL
  SELECT a.target_node_id, w.depth + 1,
         json_insert(w.node_ids, '$[#]', a.target_node_id),
         json_insert(w.assertion_ids, '$[#]', a.assertion_id),
         w.seen || a.target_node_id || ','
  FROM walk w JOIN assertion a ON a.source_node_id = w.node_id
  WHERE w.depth < ? AND a.resolution='resolved' AND a.target_node_id IS NOT NULL
    AND instr(w.seen, ',' || a.target_node_id || ',') = 0
  LIMIT ?
)
SELECT node_ids, assertion_ids, depth FROM walk WHERE node_id=? AND depth > 0
ORDER BY depth, assertion_ids LIMIT ?
""",
            (
                source_id,
                source_id,
                source_id,
                depth,
                _bound(max_rows, "max_rows", MAX_ROWS) + 1,
                target_id,
            ),
            max_rows=max_rows,
            deadline=deadline,
        )
        return [
            {
                "node_ids": json.loads(row["node_ids"]),
                "assertion_ids": json.loads(row["assertion_ids"]),
                "depth": row["depth"],
            }
            for row in rows
        ]

    def evidence(
        self,
        *,
        assertion_id: str | None = None,
        observation_id: str | None = None,
        max_rows: int = 100,
        deadline: float | None = None,
    ):
        if (assertion_id is None) == (observation_id is None):
            raise ValueError("select exactly one assertion_id or observation_id")
        column, value = (
            ("assertion_id", assertion_id)
            if assertion_id is not None
            else ("observation_id", observation_id)
        )
        rows = self._execute(
            f"SELECT e.*, s.relative_path, s.sha256 AS source_sha256 FROM evidence e "
            f"JOIN source s USING(source_id) WHERE e.{column}=? "
            "ORDER BY s.relative_path, e.byte_start, e.evidence_id LIMIT ?",
            (_text(value, column, maximum=512),),
            max_rows=max_rows,
            deadline=deadline,
        )
        return [dict(row) for row in rows]

    def unresolved(
        self,
        *,
        reason: str | None = None,
        max_rows: int = 100,
        deadline: float | None = None,
    ):
        if reason is not None and reason not in _OBSERVATION_REASONS:
            raise ValueError("reason is outside the controlled reason set")
        where = "" if reason is None else " WHERE reason=?"
        parameters: tuple[object, ...] = () if reason is None else (reason,)
        rows = self._execute(
            "SELECT observation_id, source_node_id, edge_type, target_text, reason, extractor "
            f"FROM observation{where} ORDER BY reason, edge_type, observation_id LIMIT ?",
            parameters,
            max_rows=max_rows,
            deadline=deadline,
        )
        return [dict(row) for row in rows]
