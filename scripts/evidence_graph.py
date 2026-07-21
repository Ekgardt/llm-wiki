"""Immutable Evidence Graph generation storage and bounded read queries."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import Enum, unique
from functools import lru_cache
from pathlib import Path, PurePosixPath

from code_intelligence import AnalysisIdentity, Capability, PositionEncoding, VerifiedAnalysisBatch
from reliable_memory import canonical_json_bytes, validate_runtime_file
from repository_scope import RepositoryScope

GRAPH_SCHEMA_VERSION = "evidence-graph/v2"


@unique
class GraphSchema(str, Enum):
    V2 = "evidence-graph/v2"
    V3 = "evidence-graph/v3"
MAX_DATABASE_BYTES = 16 * 1024 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024 * 1024
MAX_ROWS = 10_000
MAX_DEPTH = 32
MAX_EDGE_TYPES = 64
MAX_WORK = 100_000
PROGRESS_OPCODES = 1000
MAX_VALIDATION_ROWS = 1_000_000
MAX_SOURCE_MANIFEST_BYTES = 256 * 1024 * 1024
IO_CHUNK_BYTES = 64 * 1024
_UNSET = object()

_SHA256 = frozenset("0123456789abcdef")
_NODE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,511}")
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
_NODE_KEYS = frozenset({"node_id", "kind", "identity_scheme", "identity_key", "metadata"})
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

_TABLE_COLUMNS = {
    "source": (
        "source_id",
        "relative_path",
        "sha256",
        "size",
        "media_type",
        "language",
        "git_oid",
        "content",
    ),
    "node": ("node_id", "kind", "identity_scheme", "identity_key", "metadata_json"),
    "occurrence": (
        "occurrence_id",
        "node_id",
        "source_id",
        "role",
        "byte_start",
        "byte_end",
        "line_start",
        "line_end",
    ),
    "assertion": (
        "assertion_id",
        "source_node_id",
        "edge_type",
        "target_node_id",
        "literal_json",
        "confidence",
        "authority",
        "resolution",
        "extractor",
    ),
    "observation": (
        "observation_id",
        "source_node_id",
        "edge_type",
        "target_text",
        "reason",
        "extractor",
    ),
    "evidence": (
        "evidence_id",
        "assertion_id",
        "observation_id",
        "source_id",
        "byte_start",
        "byte_end",
        "span_sha256",
    ),
    "dependency": (
        "dependency_id",
        "dependent_node_id",
        "dependency_node_id",
        "kind",
        "source_id",
    ),
}
_EXPLICIT_INDEXES = frozenset(
    {
        "node_kind",
        "occurrence_source_span",
        "assertion_traversal",
        "assertion_reverse",
        "assertion_resolution",
        "evidence_assertion",
        "evidence_source_span",
        "observation_resolution",
        "dependency_invalidation",
        "dependency_reverse",
    }
)
_INDEX_COLUMNS = {
    "node_kind": ("kind", "identity_key", "node_id"),
    "occurrence_source_span": ("source_id", "byte_start", "byte_end", "occurrence_id"),
    "assertion_traversal": (
        "source_node_id",
        "edge_type",
        "target_node_id",
        "assertion_id",
    ),
    "assertion_reverse": (
        "target_node_id",
        "edge_type",
        "source_node_id",
        "assertion_id",
    ),
    "assertion_resolution": ("resolution", "edge_type", "assertion_id"),
    "evidence_assertion": ("assertion_id",),
    "evidence_source_span": ("source_id", "byte_start", "byte_end", "evidence_id"),
    "observation_resolution": ("reason", "edge_type", "observation_id"),
    "dependency_invalidation": (
        "dependency_node_id",
        "kind",
        "dependent_node_id",
        "dependency_id",
    ),
    "dependency_reverse": (
        "dependent_node_id",
        "kind",
        "dependency_node_id",
        "dependency_id",
    ),
}

_SCHEMA = """
CREATE TABLE source (
  source_id TEXT PRIMARY KEY,
  relative_path TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL CHECK (size >= 0),
  media_type TEXT NOT NULL,
  language TEXT,
  git_oid TEXT,
  content BLOB NOT NULL
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
CREATE INDEX evidence_assertion ON evidence(assertion_id);
CREATE INDEX evidence_source_span ON evidence(source_id, byte_start, byte_end, evidence_id);
CREATE INDEX observation_resolution ON observation(reason, edge_type, observation_id);
CREATE INDEX dependency_invalidation ON dependency(dependency_node_id, kind, dependent_node_id, dependency_id);
CREATE INDEX dependency_reverse ON dependency(dependent_node_id, kind, dependency_node_id, dependency_id);
"""

_V3_EXTENSION_SCHEMA = """
CREATE TABLE analyzer_run (
  run_id TEXT PRIMARY KEY,
  analysis_mode TEXT NOT NULL CHECK (analysis_mode IN ('precise','native-syntax')),
  repository_id TEXT NOT NULL,
  checkout_id TEXT NOT NULL,
  source_generation_id TEXT NOT NULL,
  source_manifest_sha256 TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL,
  lockfile_sha256 TEXT NOT NULL,
  sdk_sha256 TEXT NOT NULL,
  target_sha256 TEXT NOT NULL,
  configuration_sha256 TEXT NOT NULL,
  feature_sha256 TEXT NOT NULL,
  invocation_sha256 TEXT NOT NULL,
  environment_sha256 TEXT NOT NULL,
  dependency_state_sha256 TEXT NOT NULL,
  analysis_sha256 TEXT NOT NULL,
  position_encoding TEXT NOT NULL CHECK (position_encoding IN ('utf-8','utf-16','utf-32')),
  analyzer_family TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  protocol TEXT NOT NULL CHECK (protocol IN ('scip','lsp','native')),
  protocol_version TEXT NOT NULL,
  executable_sha256 TEXT NOT NULL,
  declared_capability_count INTEGER NOT NULL CHECK (declared_capability_count > 0),
  declared_capabilities_sha256 TEXT NOT NULL,
  expected_scope_count INTEGER NOT NULL CHECK (expected_scope_count > 0),
  expected_scope_set_sha256 TEXT NOT NULL,
  receipt_sha256 TEXT,
  receipt_output_sha256 TEXT,
  consent_grant_id TEXT,
  consent_revision INTEGER,
  lease_id TEXT,
  publication_generation_id TEXT NOT NULL,
  publication_expected_active TEXT,
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('compiler','semantic','syntax','lexical')),
  qualified INTEGER NOT NULL CHECK (qualified IN (0,1)),
  outcome TEXT NOT NULL CHECK (outcome IN
    ('complete','partial','failed','cancelled','rejected','superseded')),
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  UNIQUE (run_id, source_manifest_sha256),
  CHECK (
    (analysis_mode='precise' AND evidence_level IN ('compiler','semantic')
      AND receipt_sha256 IS NOT NULL AND receipt_output_sha256 IS NOT NULL
      AND consent_grant_id IS NOT NULL AND consent_revision IS NOT NULL
      AND consent_revision >= 1 AND lease_id IS NOT NULL)
    OR
    (analysis_mode='native-syntax' AND evidence_level IN ('syntax','lexical')
      AND receipt_sha256 IS NULL AND receipt_output_sha256 IS NULL
      AND consent_grant_id IS NULL AND consent_revision IS NULL AND lease_id IS NULL)
  )
) WITHOUT ROWID;

CREATE TABLE run_capability (
  run_id TEXT NOT NULL REFERENCES analyzer_run(run_id),
  capability TEXT NOT NULL CHECK (capability IN
    ('definitions','declarations','references','calls','imports','types',
     'type_definitions','inheritance','implementations','diagnostics')),
  PRIMARY KEY (run_id, capability)
) WITHOUT ROWID;

CREATE TABLE analysis_scope (
  scope_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  source_manifest_sha256 TEXT NOT NULL,
  build_target TEXT NOT NULL,
  build_configuration TEXT NOT NULL,
  expected_source_count INTEGER NOT NULL CHECK (expected_source_count >= 0),
  expected_source_set_sha256 TEXT NOT NULL,
  generated_sources TEXT NOT NULL CHECK (generated_sources IN
    ('available','unavailable','not-required')),
  dependency_resolution TEXT NOT NULL CHECK (dependency_resolution IN
    ('complete','partial','unavailable')),
  analyzer_support TEXT NOT NULL CHECK (analyzer_support IN
    ('complete','partial','unsupported','unqualified')),
  FOREIGN KEY (run_id, source_manifest_sha256)
    REFERENCES analyzer_run(run_id, source_manifest_sha256),
  UNIQUE (run_id, build_target, build_configuration),
  UNIQUE (scope_id, run_id)
) WITHOUT ROWID;

CREATE TABLE expected_source (
  scope_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES source(source_id),
  source_sha256 TEXT NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN ('included','excluded','generated')),
  PRIMARY KEY (scope_id, source_id),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id)
) WITHOUT ROWID;

CREATE TABLE coverage (
  scope_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK (capability IN
    ('definitions','declarations','references','calls','imports','types',
     'type_definitions','inheritance','implementations','diagnostics')),
  status TEXT NOT NULL CHECK (status IN
    ('complete','partial','failed','cancelled','rejected','unsupported','excluded')),
  closed_world_eligible INTEGER NOT NULL CHECK (closed_world_eligible IN (0,1)),
  reason TEXT,
  PRIMARY KEY (scope_id, source_id, capability),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  CHECK ((status IN ('complete','excluded') AND reason IS NULL)
      OR (status NOT IN ('complete','excluded') AND reason IS NOT NULL)),
  CHECK (closed_world_eligible = 0 OR status IN ('complete','excluded'))
) WITHOUT ROWID;

CREATE TABLE symbol_claim (
  claim_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK (capability IN ('definitions','declarations')),
  identity_scheme TEXT NOT NULL,
  identity_value TEXT NOT NULL,
  display_name TEXT NOT NULL,
  symbol_kind TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('definition','declaration')),
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('compiler','semantic','syntax','lexical')),
  ambiguity INTEGER NOT NULL CHECK (ambiguity IN (0,1)),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  CHECK ((capability='definitions' AND role='definition')
      OR (capability='declarations' AND role='declaration'))
) WITHOUT ROWID;

CREATE TABLE relationship_claim (
  claim_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_identity_scheme TEXT NOT NULL,
  source_identity_value TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN
    ('REFERENCES_SYMBOL','CALLS','IMPORTS','HAS_TYPE',
     'TYPE_DEFINITION','INHERITS','IMPLEMENTS')),
  capability TEXT NOT NULL CHECK (capability IN
    ('references','calls','imports','types','type_definitions','inheritance','implementations')),
  target_identity_scheme TEXT,
  target_identity_value TEXT,
  target_text TEXT,
  resolution TEXT NOT NULL CHECK (resolution IN ('resolved','unresolved','ambiguous')),
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('compiler','semantic','syntax','lexical')),
  ambiguity INTEGER NOT NULL CHECK (ambiguity IN (0,1)),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  CHECK ((resolution='resolved' AND target_identity_scheme IS NOT NULL
          AND target_identity_value IS NOT NULL AND target_text IS NULL)
      OR (resolution!='resolved' AND target_identity_scheme IS NULL
          AND target_identity_value IS NULL AND target_text IS NOT NULL)),
  CHECK ((resolution='ambiguous' AND ambiguity=1)
      OR (resolution!='ambiguous' AND ambiguity=0)),
  CHECK ((relation='REFERENCES_SYMBOL' AND capability='references')
      OR (relation='CALLS' AND capability='calls')
      OR (relation='IMPORTS' AND capability='imports')
      OR (relation='HAS_TYPE' AND capability='types')
      OR (relation='TYPE_DEFINITION' AND capability='type_definitions')
      OR (relation='INHERITS' AND capability='inheritance')
      OR (relation='IMPLEMENTS' AND capability='implementations'))
) WITHOUT ROWID;

CREATE TABLE diagnostic (
  diagnostic_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK (capability='diagnostics'),
  severity TEXT NOT NULL CHECK (severity IN ('error','warning','information','hint')),
  code TEXT,
  message TEXT NOT NULL,
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
  evidence_level TEXT NOT NULL CHECK (evidence_level IN ('compiler','semantic','syntax')),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  UNIQUE (diagnostic_id, scope_id)
) WITHOUT ROWID;

CREATE TABLE diagnostic_related (
  diagnostic_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
  scope_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  message TEXT,
  byte_start INTEGER NOT NULL CHECK (byte_start >= 0),
  byte_end INTEGER NOT NULL CHECK (byte_end > byte_start),
  PRIMARY KEY (diagnostic_id, ordinal),
  FOREIGN KEY (diagnostic_id, scope_id) REFERENCES diagnostic(diagnostic_id, scope_id),
  FOREIGN KEY (scope_id, source_id) REFERENCES expected_source(scope_id, source_id)
) WITHOUT ROWID;

CREATE TABLE slice_activation (
  slice_id TEXT PRIMARY KEY,
  slice_key TEXT NOT NULL,
  run_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  capability TEXT NOT NULL CHECK (capability IN
    ('definitions','declarations','references','calls','imports','types',
     'type_definitions','inheritance','implementations','diagnostics')),
  selected INTEGER NOT NULL CHECK (selected IN (0,1)),
  selection_reason TEXT NOT NULL CHECK (selection_reason IN
    ('new-complete','new-partial-terminal','retained-parent','complete-empty')),
  FOREIGN KEY (scope_id, run_id) REFERENCES analysis_scope(scope_id, run_id),
  FOREIGN KEY (run_id, capability) REFERENCES run_capability(run_id, capability),
  UNIQUE (slice_id, run_id, scope_id, capability)
) WITHOUT ROWID;

CREATE TABLE validity (
  validity_id TEXT PRIMARY KEY,
  symbol_claim_id TEXT REFERENCES symbol_claim(claim_id),
  relationship_claim_id TEXT REFERENCES relationship_claim(claim_id),
  diagnostic_id TEXT REFERENCES diagnostic(diagnostic_id),
  status TEXT NOT NULL CHECK (status IN ('current','soft-stale','hard-stale')),
  stale_reason TEXT,
  CHECK ((symbol_claim_id IS NOT NULL) + (relationship_claim_id IS NOT NULL)
       + (diagnostic_id IS NOT NULL) = 1),
  CHECK ((status='current' AND stale_reason IS NULL)
      OR (status!='current' AND stale_reason IS NOT NULL))
) WITHOUT ROWID;

CREATE INDEX analyzer_run_scope
ON analyzer_run(repository_id, checkout_id, analysis_sha256, run_id);
CREATE INDEX analyzer_run_publication
ON analyzer_run(publication_generation_id, outcome, run_id);
CREATE INDEX run_capability_reverse
ON run_capability(capability, run_id);
CREATE INDEX analysis_scope_run
ON analysis_scope(run_id, build_target, build_configuration, scope_id);
CREATE INDEX expected_source_reverse
ON expected_source(source_id, source_sha256, scope_id);
CREATE INDEX coverage_capability
ON coverage(scope_id, capability, status, source_id);
CREATE INDEX symbol_identity
ON symbol_claim(identity_scheme, identity_value, capability, claim_id);
CREATE INDEX symbol_source_span
ON symbol_claim(source_id, byte_start, byte_end, claim_id);
CREATE INDEX relationship_source
ON relationship_claim(source_identity_scheme, source_identity_value, capability, claim_id);
CREATE INDEX relationship_target
ON relationship_claim(target_identity_scheme, target_identity_value, capability, claim_id);
CREATE INDEX relationship_source_span
ON relationship_claim(source_id, byte_start, byte_end, claim_id);
CREATE INDEX diagnostic_source_span
ON diagnostic(source_id, byte_start, byte_end, severity, diagnostic_id);
CREATE INDEX diagnostic_related_source_span
ON diagnostic_related(source_id, byte_start, byte_end, diagnostic_id, ordinal);
CREATE UNIQUE INDEX one_selected_slice
ON slice_activation(slice_key) WHERE selected=1;
CREATE INDEX slice_run
ON slice_activation(run_id, scope_id, capability, selected, slice_id);
CREATE UNIQUE INDEX validity_symbol_once
ON validity(symbol_claim_id) WHERE symbol_claim_id IS NOT NULL;
CREATE UNIQUE INDEX validity_relationship_once
ON validity(relationship_claim_id) WHERE relationship_claim_id IS NOT NULL;
CREATE UNIQUE INDEX validity_diagnostic_once
ON validity(diagnostic_id) WHERE diagnostic_id IS NOT NULL;
CREATE INDEX validity_status
ON validity(status, validity_id);
"""

_V3_TABLES = frozenset(
    {
        "analyzer_run",
        "run_capability",
        "analysis_scope",
        "expected_source",
        "coverage",
        "symbol_claim",
        "relationship_claim",
        "diagnostic",
        "diagnostic_related",
        "slice_activation",
        "validity",
    }
)
_V3_INDEXES = frozenset(
    {
        "analyzer_run_scope",
        "analyzer_run_publication",
        "run_capability_reverse",
        "analysis_scope_run",
        "expected_source_reverse",
        "coverage_capability",
        "symbol_identity",
        "symbol_source_span",
        "relationship_source",
        "relationship_target",
        "relationship_source_span",
        "diagnostic_source_span",
        "diagnostic_related_source_span",
        "one_selected_slice",
        "slice_run",
        "validity_symbol_once",
        "validity_relationship_once",
        "validity_diagnostic_once",
        "validity_status",
    }
)


def _schema_signature(database: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in database.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    )


@lru_cache(maxsize=2)
def _expected_schema_signature(schema: GraphSchema) -> tuple[tuple[object, ...], ...]:
    database = sqlite3.connect(":memory:")
    try:
        database.executescript(
            _SCHEMA + (_V3_EXTENSION_SCHEMA if schema is GraphSchema.V3 else "")
        )
        return _schema_signature(database)
    finally:
        database.close()


def _set_sha256(rows: Sequence[Mapping[str, object]]) -> str:
    return hashlib.sha256(canonical_json_bytes(list(rows))).hexdigest()


def _slice_key(run_id: str, scope_id: str, capability: str) -> str:
    return "slice:" + hashlib.sha256(
        canonical_json_bytes(
            {"run_id": run_id, "scope_id": scope_id, "capability": capability}
        )
    ).hexdigest()


def _v3_rows(
    verified_analyses: Sequence[VerifiedAnalysisBatch],
    *,
    publication_generation_id: str,
    publication_expected_active: str | None,
    repository_scope: RepositoryScope,
) -> dict[str, list[tuple[object, ...]]]:
    rows: dict[str, list[tuple[object, ...]]] = {
        table: []
        for table in (
            "analyzer_run",
            "run_capability",
            "analysis_scope",
            "expected_source",
            "coverage",
            "symbol_claim",
            "relationship_claim",
            "diagnostic",
            "diagnostic_related",
            "slice_activation",
            "validity",
        )
    }
    total_rows = 0
    for batch in verified_analyses:
        if type(batch) is not VerifiedAnalysisBatch:
            raise TypeError("verified_analyses must contain VerifiedAnalysisBatch values")
        analysis = batch.analysis
        run = analysis.run
        if (run.repository_id, run.checkout_id) != (
            repository_scope.repository_id,
            repository_scope.checkout_id,
        ):
            raise ValueError("analyzer run repository or checkout does not match publication scope")
        identity = run.identity
        related_count = sum(len(item.related) for item in analysis.diagnostics)
        expected_source_count = sum(len(scope.expected_sources) for scope in analysis.scopes)
        additions = {
            "analyzer_run": 1,
            "run_capability": len(run.declared_capabilities),
            "analysis_scope": len(analysis.scopes),
            "expected_source": expected_source_count,
            "coverage": len(analysis.coverage),
            "symbol_claim": len(analysis.symbols),
            "relationship_claim": len(analysis.relationships),
            "diagnostic": len(analysis.diagnostics),
            "diagnostic_related": related_count,
            "slice_activation": len(analysis.scopes) * len(run.declared_capabilities),
            "validity": len(analysis.validity),
        }
        for table, count in additions.items():
            if count > MAX_VALIDATION_ROWS - len(rows[table]):
                raise ValueError(f"{table} row ceiling exceeded")
        total_rows += sum(additions.values())
        if total_rows > MAX_VALIDATION_ROWS * len(rows):
            raise ValueError("v3 aggregate row ceiling exceeded")
        capability_rows = [
            {"capability": capability.value}
            for capability in run.declared_capabilities
        ]
        scope_rows = [
            {
                "scope_id": scope.scope_id,
                "source_manifest_sha256": scope.source_manifest_sha256,
                "target": scope.build_target,
                "configuration": scope.build_configuration,
            }
            for scope in analysis.scopes
        ]
        if any(
            scope.source_manifest_sha256 != run.source_manifest_sha256
            for scope in analysis.scopes
        ):
            raise ValueError("analysis scope manifest must match its analyzer run")
        rows["analyzer_run"].append(
            (
                run.run_id,
                run.analysis_mode,
                run.repository_id,
                run.checkout_id,
                run.source_generation_id,
                run.source_manifest_sha256,
                identity.manifest_sha256,
                identity.lockfile_sha256,
                identity.sdk_sha256,
                identity.target_sha256,
                identity.configuration_sha256,
                identity.feature_sha256,
                identity.invocation_sha256,
                identity.environment_sha256,
                identity.dependency_state_sha256,
                identity.analysis_sha256,
                identity.position_encoding.value,
                run.analyzer_family,
                run.analyzer_version,
                run.protocol,
                run.protocol_version,
                run.executable_sha256,
                len(capability_rows),
                _set_sha256(capability_rows),
                len(scope_rows),
                _set_sha256(scope_rows),
                run.receipt_sha256,
                run.receipt_output_sha256,
                run.consent_grant_id,
                run.consent_revision,
                run.lease_id,
                publication_generation_id,
                publication_expected_active,
                run.evidence_level.value,
                int(run.qualified),
                run.outcome.value,
                run.started_at,
                run.ended_at,
            )
        )
        rows["run_capability"].extend(
            (run.run_id, capability.value)
            for capability in run.declared_capabilities
        )
        scope_run = {scope.scope_id: scope.run_id for scope in analysis.scopes}
        for scope in analysis.scopes:
            source_rows = [
                {
                    "source_id": source.source_id,
                    "sha256": source.source_sha256,
                    "disposition": source.disposition,
                }
                for source in scope.expected_sources
            ]
            rows["analysis_scope"].append(
                (
                    scope.scope_id,
                    scope.run_id,
                    scope.source_manifest_sha256,
                    scope.build_target,
                    scope.build_configuration,
                    len(source_rows),
                    _set_sha256(source_rows),
                    scope.generated_sources,
                    scope.dependency_resolution,
                    scope.analyzer_support,
                )
            )
            rows["expected_source"].extend(
                (
                    scope.scope_id,
                    scope.run_id,
                    source.source_id,
                    source.source_sha256,
                    source.disposition,
                )
                for source in scope.expected_sources
            )
            for capability in run.declared_capabilities:
                slice_key = _slice_key(run.run_id, scope.scope_id, capability.value)
                rows["slice_activation"].append(
                    (
                        slice_key,
                        slice_key,
                        run.run_id,
                        scope.scope_id,
                        capability.value,
                        1,
                        "new-complete"
                        if run.outcome.value == "complete"
                        else "new-partial-terminal",
                    )
                )
        rows["coverage"].extend(
            (
                coverage.scope_id,
                scope_run[coverage.scope_id],
                coverage.source_id,
                coverage.capability.value,
                coverage.status.value,
                int(coverage.closed_world_eligible),
                coverage.reason,
            )
            for coverage in analysis.coverage
        )
        rows["symbol_claim"].extend(
            (
                claim.claim_id,
                claim.run_id,
                claim.scope_id,
                claim.source_id,
                claim.capability.value,
                claim.identity.scheme,
                claim.identity.value,
                claim.display_name,
                claim.symbol_kind,
                claim.role.value,
                claim.range.byte_start,
                claim.range.byte_end,
                claim.evidence_level.value,
                int(claim.ambiguity),
            )
            for claim in analysis.symbols
        )
        rows["relationship_claim"].extend(
            (
                claim.claim_id,
                claim.run_id,
                claim.scope_id,
                claim.source_id,
                claim.source_identity.scheme,
                claim.source_identity.value,
                claim.relation.value,
                claim.capability.value,
                None if claim.target_identity is None else claim.target_identity.scheme,
                None if claim.target_identity is None else claim.target_identity.value,
                claim.target_text,
                claim.resolution.value,
                claim.range.byte_start,
                claim.range.byte_end,
                claim.evidence_level.value,
                int(claim.ambiguity),
            )
            for claim in analysis.relationships
        )
        for diagnostic in analysis.diagnostics:
            rows["diagnostic"].append(
                (
                    diagnostic.diagnostic_id,
                    diagnostic.run_id,
                    diagnostic.scope_id,
                    diagnostic.source_id,
                    diagnostic.capability.value,
                    diagnostic.severity.value,
                    diagnostic.code,
                    diagnostic.message,
                    diagnostic.range.byte_start,
                    diagnostic.range.byte_end,
                    diagnostic.evidence_level.value,
                )
            )
            rows["diagnostic_related"].extend(
                (
                    diagnostic.diagnostic_id,
                    ordinal,
                    diagnostic.scope_id,
                    related.source_id,
                    related.message,
                    related.range.byte_start,
                    related.range.byte_end,
                )
                for ordinal, related in enumerate(diagnostic.related)
            )
        for validity in analysis.validity:
            subject = [None, None, None]
            subject[{"symbol": 0, "relationship": 1, "diagnostic": 2}[validity.subject_kind.value]] = (
                validity.subject_id
            )
            rows["validity"].append(
                (
                    validity.validity_id,
                    *subject,
                    validity.status.value,
                    validity.stale_reason,
                )
            )
    return rows


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


def _node_id(value: object, label: str = "node_id") -> str:
    text = _text(value, label, maximum=512)
    assert text is not None
    if _NODE_ID.fullmatch(text) is None:
        raise ValueError(f"{label} must use the closed delimiter-safe identifier syntax")
    return text


def _integer(
    value: object, label: str, *, minimum: int = 0, maximum: int = MAX_SOURCE_BYTES
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside its supported range")
    return value


def _relative_path(value: object) -> str:
    text = _text(value, "relative_path")
    assert text is not None
    if "\\" in text:
        raise ValueError("relative_path must use normalized POSIX separators")
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or text != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
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


def _validate_occurrence_lines(
    content: bytes,
    start: int,
    end: int,
    line_start: int,
    line_end: int,
) -> None:
    if end <= start:
        raise ValueError("occurrence byte range must be non-empty")
    expected_start = content.count(b"\n", 0, start) + 1
    expected_end = content.count(b"\n", 0, end) + 1
    if (line_start, line_end) != (expected_start, expected_end):
        raise ValueError("occurrence line range does not match its captured source bytes")


def _check_build_stop(
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    monotonic: Callable[[], float],
) -> None:
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")
    if bool(cancelled and cancelled()):
        raise TimeoutError("Evidence Graph construction cancelled")
    if deadline is not None and monotonic() >= deadline:
        raise TimeoutError("Evidence Graph construction deadline reached")


def _hash_bytes_stopped(
    content: bytes,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    monotonic: Callable[[], float],
) -> str:
    digest = hashlib.sha256()
    for offset in range(0, len(content), IO_CHUNK_BYTES):
        _check_build_stop(deadline, cancelled, monotonic)
        digest.update(content[offset : offset + IO_CHUNK_BYTES])
    _check_build_stop(deadline, cancelled, monotonic)
    return digest.hexdigest()


def _ordered_stopped(
    records: Iterable[Mapping[str, object]],
    key: str,
    label: str,
    *,
    deadline: float | None,
    cancelled: Callable[[], bool] | None,
    monotonic: Callable[[], float],
) -> list[Mapping[str, object]]:
    values: list[Mapping[str, object]] = []
    for record in records:
        _check_build_stop(deadline, cancelled, monotonic)
        values.append(record)
        if len(values) > MAX_VALIDATION_ROWS:
            raise ValueError(f"{label} row ceiling exceeded")
    _check_build_stop(deadline, cancelled, monotonic)
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
    schema: GraphSchema = GraphSchema.V2,
    sources: Iterable[Mapping[str, object]],
    source_bytes: Mapping[str, bytes],
    nodes: Iterable[Mapping[str, object]],
    occurrences: Iterable[Mapping[str, object]],
    assertions: Iterable[Mapping[str, object]],
    evidence: Iterable[Mapping[str, object]],
    observations: Iterable[Mapping[str, object]],
    dependencies: Iterable[Mapping[str, object]],
    verified_analyses: Iterable[VerifiedAnalysisBatch] = (),
    publication_generation_id: str | None = None,
    publication_expected_active: str | None = None,
    repository_scope: RepositoryScope | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> GraphSchema:
    """Create one immutable database using only the explicitly selected schema."""
    if not isinstance(schema, GraphSchema):
        raise TypeError("schema must be a GraphSchema")
    verified_values: list[VerifiedAnalysisBatch] = []
    for batch in verified_analyses:
        if len(verified_values) >= MAX_VALIDATION_ROWS:
            raise ValueError("verified analysis row ceiling exceeded")
        verified_values.append(batch)
    verified = tuple(verified_values)
    if schema is GraphSchema.V2 and verified:
        raise ValueError("verified analyses require explicit evidence-graph/v3")
    if schema is GraphSchema.V3:
        generation_id = _text(
            publication_generation_id, "publication_generation_id", maximum=128
        )
        expected_active = _text(
            publication_expected_active,
            "publication_expected_active",
            maximum=128,
            optional=True,
        )
        if not isinstance(repository_scope, RepositoryScope):
            raise TypeError("repository_scope must be a RepositoryScope for evidence-graph/v3")
        repository_scope = RepositoryScope.from_dict(repository_scope.as_dict())
        assert generation_id is not None
        extension_rows = _v3_rows(
            verified,
            publication_generation_id=generation_id,
            publication_expected_active=expected_active,
            repository_scope=repository_scope,
        )
    else:
        if any(
            value is not None
            for value in (
                publication_generation_id,
                publication_expected_active,
                repository_scope,
            )
        ):
            raise ValueError("publication context is only valid for evidence-graph/v3")
        extension_rows = {}
    path = Path(database_path)
    if path.exists() or path.is_symlink():
        raise FileExistsError("Evidence Graph generation artifacts are immutable")
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve(strict=True)
    metadata = path.parent.lstat()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & reparse:
        raise PermissionError(
            "Evidence Graph generation directory must not be a link or reparse point"
        )
    temporary = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    source_content: dict[str, bytes] = {}
    try:
        _check_build_stop(deadline, cancelled, monotonic)
        source_rows = _ordered_stopped(
            sources,
            "source_id",
            "source",
            deadline=deadline,
            cancelled=cancelled,
            monotonic=monotonic,
        )
        if set(source_bytes) != {record.get("source_id") for record in source_rows}:
            raise ValueError("source_bytes must bind every captured source exactly once")
        normalized_sources = []
        for record in source_rows:
            _check_build_stop(deadline, cancelled, monotonic)
            _closed(record, _SOURCE_KEYS, "source")
            source_id = _text(record["source_id"], "source_id", maximum=512)
            assert source_id is not None
            content = source_bytes[source_id]
            if not isinstance(content, bytes) or len(content) > MAX_SOURCE_BYTES:
                raise TypeError("captured source content must be bounded bytes")
            size = _integer(record["size"], "source size")
            digest = _digest(record["sha256"], "source hash")
            if size != len(content) or digest != _hash_bytes_stopped(
                content,
                deadline=deadline,
                cancelled=cancelled,
                monotonic=monotonic,
            ):
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
                    content,
                )
            )

        normalized_nodes = []
        for record in _ordered_stopped(
            nodes, "node_id", "node", deadline=deadline, cancelled=cancelled, monotonic=monotonic
        ):
            _check_build_stop(deadline, cancelled, monotonic)
            _closed(record, _NODE_KEYS, "node")
            normalized_nodes.append(
                (
                    _node_id(record["node_id"]),
                    _text(record["kind"], "node kind", maximum=128),
                    _text(record["identity_scheme"], "identity_scheme", maximum=256),
                    _text(record["identity_key"], "identity_key", maximum=4096),
                    _canonical_json(record["metadata"], "node metadata"),
                )
            )

        normalized_occurrences = []
        for record in _ordered_stopped(
            occurrences,
            "occurrence_id",
            "occurrence",
            deadline=deadline,
            cancelled=cancelled,
            monotonic=monotonic,
        ):
            _check_build_stop(deadline, cancelled, monotonic)
            _closed(record, _OCCURRENCE_KEYS, "occurrence")
            source_id = _text(record["source_id"], "source_id", maximum=512)
            assert source_id is not None
            start = _integer(record["byte_start"], "occurrence byte_start")
            end = _integer(record["byte_end"], "occurrence byte_end", minimum=start)
            if source_id not in source_content or end > len(source_content[source_id]):
                raise ValueError("occurrence byte range is outside its captured source")
            line_start = _integer(record["line_start"], "line_start", minimum=1, maximum=2**31 - 1)
            line_end = _integer(
                record["line_end"], "line_end", minimum=line_start, maximum=2**31 - 1
            )
            _validate_occurrence_lines(source_content[source_id], start, end, line_start, line_end)
            normalized_occurrences.append(
                (
                    _text(record["occurrence_id"], "occurrence_id", maximum=512),
                    None if record["node_id"] is None else _node_id(record["node_id"]),
                    source_id,
                    _text(record["role"], "occurrence role", maximum=128),
                    start,
                    end,
                    line_start,
                    line_end,
                )
            )

        normalized_assertions = []
        resolved_assertions: set[str] = set()
        for record in _ordered_stopped(
            assertions,
            "assertion_id",
            "assertion",
            deadline=deadline,
            cancelled=cancelled,
            monotonic=monotonic,
        ):
            _check_build_stop(deadline, cancelled, monotonic)
            _closed(record, _ASSERTION_KEYS, "assertion")
            resolution = record["resolution"]
            if resolution not in _RESOLUTION:
                raise ValueError("assertion resolution is outside the closed contract")
            if resolution != "resolved":
                raise ValueError("unresolved assertions must use a controlled observation")
            target = _text(record["target_node_id"], "target_node_id", maximum=512, optional=True)
            literal = (
                None if record["literal"] is None else _canonical_json(record["literal"], "literal")
            )
            if (target is None) == (literal is None):
                raise ValueError("assertion must have exactly one target node or literal")
            confidence = record["confidence"]
            authority = record["authority"]
            if confidence not in _CONFIDENCE or authority not in _AUTHORITY:
                raise ValueError("assertion confidence or authority is outside the closed contract")
            assertion_id = _text(record["assertion_id"], "assertion_id", maximum=512)
            assert assertion_id is not None
            if resolution == "resolved":
                resolved_assertions.add(assertion_id)
            normalized_assertions.append(
                (
                    assertion_id,
                    _node_id(record["source_node_id"], "source_node_id"),
                    _text(record["edge_type"], "edge_type", maximum=128),
                    None if target is None else _node_id(target, "target_node_id"),
                    literal,
                    confidence,
                    authority,
                    resolution,
                    _text(record["extractor"], "extractor", maximum=256),
                )
            )

        normalized_observations = []
        for record in _ordered_stopped(
            observations,
            "observation_id",
            "observation",
            deadline=deadline,
            cancelled=cancelled,
            monotonic=monotonic,
        ):
            _check_build_stop(deadline, cancelled, monotonic)
            _closed(record, _OBSERVATION_KEYS, "observation")
            if record["reason"] not in _OBSERVATION_REASONS:
                raise ValueError("observation reason is outside the controlled reason set")
            normalized_observations.append(
                (
                    _text(record["observation_id"], "observation_id", maximum=512),
                    None
                    if record["source_node_id"] is None
                    else _node_id(record["source_node_id"], "source_node_id"),
                    _text(record["edge_type"], "edge_type", maximum=128),
                    _text(record["target_text"], "target_text", optional=True),
                    record["reason"],
                    _text(record["extractor"], "extractor", maximum=256),
                )
            )

        normalized_evidence = []
        evidenced_assertions: set[str] = set()
        for record in _ordered_stopped(
            evidence,
            "evidence_id",
            "evidence",
            deadline=deadline,
            cancelled=cancelled,
            monotonic=monotonic,
        ):
            _check_build_stop(deadline, cancelled, monotonic)
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
            end = _integer(record["byte_end"], "evidence byte_end", minimum=start + 1)
            if source_id not in source_content or end > len(source_content[source_id]):
                raise ValueError("evidence byte range is outside its captured source")
            span_hash = _digest(record["span_sha256"], "evidence span hash")
            if (
                _hash_bytes_stopped(
                    source_content[source_id][start:end],
                    deadline=deadline,
                    cancelled=cancelled,
                    monotonic=monotonic,
                )
                != span_hash
            ):
                raise ValueError("evidence span hash does not match the captured source range")
            if assertion_id is not None:
                evidenced_assertions.add(assertion_id)
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

        if missing := resolved_assertions - evidenced_assertions:
            raise ValueError(
                f"every resolved assertion requires evidence; missing: {sorted(missing)!r}"
            )

        normalized_dependencies = []
        for record in _ordered_stopped(
            dependencies,
            "dependency_id",
            "dependency",
            deadline=deadline,
            cancelled=cancelled,
            monotonic=monotonic,
        ):
            _check_build_stop(deadline, cancelled, monotonic)
            _closed(record, _DEPENDENCY_KEYS, "dependency")
            normalized_dependencies.append(
                (
                    _text(record["dependency_id"], "dependency_id", maximum=512),
                    _node_id(record["dependent_node_id"], "dependent_node_id"),
                    _node_id(record["dependency_node_id"], "dependency_node_id"),
                    _text(record["kind"], "dependency kind", maximum=128),
                    _text(record["source_id"], "source_id", maximum=512, optional=True),
                )
            )

        database = sqlite3.connect(temporary)
        try:
            _configure_write(database)
            database.set_progress_handler(
                lambda: int(
                    bool(cancelled and cancelled())
                    or (deadline is not None and monotonic() >= deadline)
                ),
                PROGRESS_OPCODES,
            )
            database.executescript(
                "BEGIN IMMEDIATE;\n"
                + _SCHEMA
                + (_V3_EXTENSION_SCHEMA if schema is GraphSchema.V3 else "")
            )
            database.executemany(
                "INSERT INTO source VALUES (?, ?, ?, ?, ?, ?, ?, ?)", normalized_sources
            )
            database.executemany("INSERT INTO node VALUES (?, ?, ?, ?, ?)", normalized_nodes)
            database.executemany(
                "INSERT INTO occurrence VALUES (?, ?, ?, ?, ?, ?, ?, ?)", normalized_occurrences
            )
            database.executemany(
                "INSERT INTO assertion VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", normalized_assertions
            )
            database.executemany(
                "INSERT INTO observation VALUES (?, ?, ?, ?, ?, ?)", normalized_observations
            )
            database.executemany(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)", normalized_evidence
            )
            database.executemany(
                "INSERT INTO dependency VALUES (?, ?, ?, ?, ?)", normalized_dependencies
            )
            if schema is GraphSchema.V3:
                for table in (
                    "analyzer_run",
                    "run_capability",
                    "analysis_scope",
                    "expected_source",
                    "coverage",
                    "symbol_claim",
                    "relationship_claim",
                    "diagnostic",
                    "diagnostic_related",
                    "slice_activation",
                    "validity",
                ):
                    table_rows = extension_rows[table]
                    if table_rows:
                        slots = ", ".join("?" for _ in table_rows[0])
                        database.executemany(
                            f"INSERT INTO {table} VALUES ({slots})", table_rows
                        )
            database.execute(f"PRAGMA user_version={2 if schema is GraphSchema.V2 else 3}")
            violations = database.execute("PRAGMA foreign_key_check").fetchone()
            if violations is not None:
                raise ValueError("Evidence Graph records violate referential integrity")
            database.commit()
            _check_build_stop(deadline, cancelled, monotonic)
        except sqlite3.OperationalError as exc:
            if bool(cancelled and cancelled()) or (
                deadline is not None and monotonic() >= deadline
            ):
                raise TimeoutError(
                    "Evidence Graph construction cancelled or deadline reached"
                ) from exc
            raise
        except BaseException:
            database.rollback()
            raise
        finally:
            database.set_progress_handler(None, 0)
            database.close()
        validate_generation_database(
            temporary,
            schema=schema,
            publication_generation_id=publication_generation_id,
            publication_expected_active=(
                publication_expected_active if schema is GraphSchema.V3 else _UNSET
            ),
            repository_scope=repository_scope,
        )
        if temporary.stat().st_size > MAX_DATABASE_BYTES:
            raise ValueError("Evidence Graph database exceeds the supported byte ceiling")
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise FileExistsError("Evidence Graph generation artifacts are immutable") from None
        temporary.unlink()
        return schema
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


def _edge_type_values(edge_types: Sequence[str] | None) -> tuple[str, ...]:
    if edge_types is None:
        return ()
    if isinstance(edge_types, (str, bytes)) or not isinstance(edge_types, Sequence):
        raise ValueError("edge_types must be a bounded sequence")
    if len(edge_types) > MAX_EDGE_TYPES:
        raise ValueError(f"edge_types cannot contain more than {MAX_EDGE_TYPES} values")
    return tuple(sorted({_text(value, "edge_type", maximum=128) for value in edge_types}))


def _validation_deadline(
    database: sqlite3.Connection,
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None = None,
) -> None:
    if deadline is not None and (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
    ):
        raise ValueError("deadline must be a finite monotonic timestamp")
    _check_build_stop(deadline, cancelled, monotonic)
    database.set_progress_handler(
        None
        if deadline is None and cancelled is None
        else lambda: int(
            bool(cancelled and cancelled()) or (deadline is not None and monotonic() >= deadline)
        ),
        PROGRESS_OPCODES,
    )


def _stored_json(value: object, label: str, *, optional: bool = False) -> object:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be canonical JSON text")
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be canonical JSON text") from exc
    if _canonical_json(decoded, label) != value:
        raise ValueError(f"{label} must be canonical JSON text")
    return decoded


def _validate_stored_records(
    database: sqlite3.Connection,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Replay creation-time field and source-byte validation for persisted rows."""
    source_content: dict[str, bytes] = {}
    for row in database.execute("SELECT * FROM source ORDER BY source_id"):
        _check_build_stop(deadline, cancelled, monotonic)
        source_id = _text(row["source_id"], "source_id", maximum=512)
        assert source_id is not None
        content = row["content"]
        if not isinstance(content, bytes) or len(content) > MAX_SOURCE_BYTES:
            raise ValueError("captured source content must be bounded bytes")
        size = _integer(row["size"], "source size")
        digest = _digest(row["sha256"], "source hash")
        if size != len(content) or digest != _hash_bytes_stopped(
            content,
            deadline=deadline,
            cancelled=cancelled,
            monotonic=monotonic,
        ):
            raise ValueError("captured source manifest size or hash does not match source bytes")
        _relative_path(row["relative_path"])
        _text(row["media_type"], "media_type", maximum=256)
        _text(row["language"], "language", maximum=128, optional=True)
        _text(row["git_oid"], "git_oid", maximum=128, optional=True)
        source_content[source_id] = content

    for row in database.execute("SELECT * FROM node ORDER BY node_id"):
        _check_build_stop(deadline, cancelled, monotonic)
        _node_id(row["node_id"])
        _text(row["kind"], "node kind", maximum=128)
        _text(row["identity_scheme"], "identity_scheme", maximum=256)
        _text(row["identity_key"], "identity_key", maximum=4096)
        _stored_json(row["metadata_json"], "node metadata")

    for row in database.execute("SELECT * FROM occurrence ORDER BY occurrence_id"):
        _check_build_stop(deadline, cancelled, monotonic)
        _text(row["occurrence_id"], "occurrence_id", maximum=512)
        if row["node_id"] is not None:
            _node_id(row["node_id"])
        source_id = _text(row["source_id"], "source_id", maximum=512)
        assert source_id is not None
        _text(row["role"], "occurrence role", maximum=128)
        start = _integer(row["byte_start"], "occurrence byte_start")
        end = _integer(row["byte_end"], "occurrence byte_end", minimum=start)
        if source_id not in source_content or end > len(source_content[source_id]):
            raise ValueError("occurrence byte range is outside its captured source")
        line_start = _integer(row["line_start"], "line_start", minimum=1, maximum=2**31 - 1)
        line_end = _integer(row["line_end"], "line_end", minimum=line_start, maximum=2**31 - 1)
        _validate_occurrence_lines(source_content[source_id], start, end, line_start, line_end)

    resolved_assertions: set[str] = set()
    for row in database.execute("SELECT * FROM assertion ORDER BY assertion_id"):
        _check_build_stop(deadline, cancelled, monotonic)
        assertion_id = _text(row["assertion_id"], "assertion_id", maximum=512)
        assert assertion_id is not None
        _node_id(row["source_node_id"], "source_node_id")
        _text(row["edge_type"], "edge_type", maximum=128)
        resolution = row["resolution"]
        if resolution not in _RESOLUTION:
            raise ValueError("assertion resolution is outside the closed contract")
        if resolution != "resolved":
            raise ValueError("unresolved assertions must use a controlled observation")
        target = row["target_node_id"]
        if target is not None:
            _node_id(target, "target_node_id")
        literal = _stored_json(row["literal_json"], "literal", optional=True)
        if (target is None) == (literal is None):
            raise ValueError("assertion must have exactly one target node or literal")
        if row["confidence"] not in _CONFIDENCE or row["authority"] not in _AUTHORITY:
            raise ValueError("assertion confidence or authority is outside the closed contract")
        _text(row["extractor"], "extractor", maximum=256)
        if resolution == "resolved":
            resolved_assertions.add(assertion_id)

    for row in database.execute("SELECT * FROM observation ORDER BY observation_id"):
        _check_build_stop(deadline, cancelled, monotonic)
        _text(row["observation_id"], "observation_id", maximum=512)
        if row["source_node_id"] is not None:
            _node_id(row["source_node_id"], "source_node_id")
        _text(row["edge_type"], "edge_type", maximum=128)
        _text(row["target_text"], "target_text", optional=True)
        if row["reason"] not in _OBSERVATION_REASONS:
            raise ValueError("observation reason is outside the controlled reason set")
        _text(row["extractor"], "extractor", maximum=256)

    evidenced_assertions: set[str] = set()
    for row in database.execute("SELECT * FROM evidence ORDER BY evidence_id"):
        _check_build_stop(deadline, cancelled, monotonic)
        _text(row["evidence_id"], "evidence_id", maximum=512)
        assertion_id = _text(row["assertion_id"], "assertion_id", maximum=512, optional=True)
        observation_id = _text(row["observation_id"], "observation_id", maximum=512, optional=True)
        if (assertion_id is None) == (observation_id is None):
            raise ValueError("evidence must bind exactly one assertion or observation")
        source_id = _text(row["source_id"], "source_id", maximum=512)
        assert source_id is not None
        start = _integer(row["byte_start"], "evidence byte_start")
        end = _integer(row["byte_end"], "evidence byte_end", minimum=start + 1)
        if source_id not in source_content or end > len(source_content[source_id]):
            raise ValueError("evidence byte range is outside its captured source")
        span_hash = _digest(row["span_sha256"], "evidence span hash")
        if (
            _hash_bytes_stopped(
                source_content[source_id][start:end],
                deadline=deadline,
                cancelled=cancelled,
                monotonic=monotonic,
            )
            != span_hash
        ):
            raise ValueError("evidence span hash does not match the captured source range")
        if assertion_id is not None:
            evidenced_assertions.add(assertion_id)
    if resolved_assertions - evidenced_assertions:
        raise ValueError("every resolved assertion requires evidence")

    for row in database.execute("SELECT * FROM dependency ORDER BY dependency_id"):
        _check_build_stop(deadline, cancelled, monotonic)
        _text(row["dependency_id"], "dependency_id", maximum=512)
        _node_id(row["dependent_node_id"], "dependent_node_id")
        _node_id(row["dependency_node_id"], "dependency_node_id")
        _text(row["kind"], "dependency kind", maximum=128)
        _text(row["source_id"], "source_id", maximum=512, optional=True)

    _check_build_stop(deadline, cancelled, monotonic)


def validate_persisted_scope(database: sqlite3.Connection, run_id: str) -> None:
    run = database.execute(
        "SELECT source_manifest_sha256,expected_scope_count,expected_scope_set_sha256,"
        "declared_capability_count,declared_capabilities_sha256 "
        "FROM analyzer_run WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ValueError("analyzer run is missing")
    scopes = database.execute(
        "SELECT scope_id,source_manifest_sha256,build_target,build_configuration "
        "FROM analysis_scope WHERE run_id=? ORDER BY scope_id",
        (run_id,),
    ).fetchall()
    scope_rows = [
        {
            "scope_id": row[0],
            "source_manifest_sha256": row[1],
            "target": row[2],
            "configuration": row[3],
        }
        for row in scopes
    ]
    if (
        any(row[1] != run[0] for row in scopes)
        or len(scope_rows) != run[1]
        or _set_sha256(scope_rows) != run[2]
    ):
        raise ValueError("persisted expected scope set is incomplete")
    capabilities = [
        {"capability": row[0]}
        for row in database.execute(
            "SELECT capability FROM run_capability WHERE run_id=? ORDER BY capability",
            (run_id,),
        ).fetchall()
    ]
    if len(capabilities) != run[3] or _set_sha256(capabilities) != run[4]:
        raise ValueError("persisted declared capability set is incomplete")
    capability_values = [row["capability"] for row in capabilities]
    for scope_id, _manifest, _target, _configuration in scopes:
        expected_count, expected_sha256 = database.execute(
            "SELECT expected_source_count,expected_source_set_sha256 "
            "FROM analysis_scope WHERE scope_id=?",
            (scope_id,),
        ).fetchone()
        sources = database.execute(
            "SELECT source_id,source_sha256,disposition FROM expected_source "
            "WHERE scope_id=? ORDER BY source_id",
            (scope_id,),
        ).fetchall()
        source_rows = [
            {"source_id": row[0], "sha256": row[1], "disposition": row[2]}
            for row in sources
        ]
        if len(source_rows) != expected_count or _set_sha256(source_rows) != expected_sha256:
            raise ValueError("persisted expected source set is incomplete")
        for source_id, source_sha256, _disposition in sources:
            stored = database.execute(
                "SELECT sha256 FROM source WHERE source_id=?", (source_id,)
            ).fetchone()
            if stored is None or stored[0] != source_sha256:
                raise ValueError("persisted expected source hash does not match captured source")
        expected_coverage = {
            (source_id, capability)
            for source_id, _sha256, _disposition in sources
            for capability in capability_values
        }
        actual_coverage = {
            (row[0], row[1])
            for row in database.execute(
                "SELECT source_id,capability FROM coverage WHERE scope_id=?",
                (scope_id,),
            ).fetchall()
        }
        coverage_count = database.execute(
            "SELECT count(*) FROM coverage WHERE scope_id=?", (scope_id,)
        ).fetchone()[0]
        if actual_coverage != expected_coverage or coverage_count != len(expected_coverage):
            raise ValueError("coverage does not span expected sources and capabilities")


def database_closed_world(
    database: sqlite3.Connection,
    scope_id: str,
    capability: Capability,
) -> bool:
    if not isinstance(capability, Capability):
        return False
    scope = database.execute(
        "SELECT expected_source_count,expected_source_set_sha256,generated_sources,"
        "dependency_resolution,analyzer_support FROM analysis_scope WHERE scope_id=?",
        (scope_id,),
    ).fetchone()
    if scope is None:
        return False
    sources = database.execute(
        "SELECT source_id,source_sha256,disposition FROM expected_source "
        "WHERE scope_id=? ORDER BY source_id",
        (scope_id,),
    ).fetchall()
    source_rows = [
        {"source_id": row[0], "sha256": row[1], "disposition": row[2]}
        for row in sources
    ]
    if len(source_rows) != scope[0] or _set_sha256(source_rows) != scope[1]:
        return False
    coverage = database.execute(
        "SELECT source_id,status,closed_world_eligible FROM coverage "
        "WHERE scope_id=? AND capability=? ORDER BY source_id",
        (scope_id, capability.value),
    ).fetchall()
    if [row[0] for row in coverage] != [row["source_id"] for row in source_rows]:
        return False
    return (
        scope[2] in {"available", "not-required"}
        and scope[3] == "complete"
        and scope[4] == "complete"
        and all(row[1] in {"complete", "excluded"} and row[2] == 1 for row in coverage)
    )


def _validate_v3_sets(
    database: sqlite3.Connection,
    *,
    publication_generation_id: str | None = None,
    publication_expected_active: object = _UNSET,
    repository_scope: RepositoryScope | None = None,
    source_manifest_sha256: str | None = None,
) -> None:
    run_rows = database.execute("SELECT * FROM analyzer_run ORDER BY run_id").fetchall()
    run_ids = [row["run_id"] for row in run_rows]
    component_names = AnalysisIdentity._component_names()
    for row in run_rows:
        identity = AnalysisIdentity(
            **{name: _digest(row[name], name) for name in component_names},
            position_encoding=PositionEncoding(row["position_encoding"]),
            analysis_sha256=_digest(row["analysis_sha256"], "analysis_sha256"),
        )
        if identity.recompute_analysis_sha256() != identity.analysis_sha256:
            raise ValueError("persisted analysis_sha256 does not match its components")
        for column in (
            "executable_sha256",
            "declared_capabilities_sha256",
            "expected_scope_set_sha256",
        ):
            _digest(row[column], column)
        for column in ("receipt_sha256", "receipt_output_sha256"):
            if row[column] is not None:
                _digest(row[column], column)
        if source_manifest_sha256 is not None and row["source_manifest_sha256"] != source_manifest_sha256:
            raise ValueError("analyzer run source manifest does not match generation manifest")
        if publication_generation_id is not None and (
            row["publication_generation_id"] != publication_generation_id
        ):
            raise ValueError("analyzer run publication generation does not match generation manifest")
        if publication_expected_active is not _UNSET and (
            row["publication_expected_active"] != publication_expected_active
        ):
            raise ValueError("analyzer run expected active generation does not match publication")
        if repository_scope is not None and (
            row["repository_id"],
            row["checkout_id"],
        ) != (repository_scope.repository_id, repository_scope.checkout_id):
            raise ValueError("analyzer run repository or checkout does not match publication scope")

    for run_id in run_ids:
        validate_persisted_scope(database, run_id)

    for table in ("symbol_claim", "relationship_claim", "diagnostic"):
        invalid_range = database.execute(
            f"SELECT 1 FROM {table} c JOIN source s USING(source_id) "
            "WHERE c.byte_start < 0 OR c.byte_end <= c.byte_start "
            "OR c.byte_end > s.size LIMIT 1"
        ).fetchone()
        if invalid_range is not None:
            raise ValueError(f"persisted {table} range is outside its captured source")
    invalid_related_range = database.execute(
        "SELECT 1 FROM diagnostic_related r JOIN source s USING(source_id) "
        "WHERE r.byte_start < 0 OR r.byte_end <= r.byte_start "
        "OR r.byte_end > s.size LIMIT 1"
    ).fetchone()
    if invalid_related_range is not None:
        raise ValueError("persisted diagnostic_related range is outside its captured source")

    required_slices = {
        (_slice_key(row[0], row[1], row[2]), row[0], row[1], row[2])
        for row in database.execute(
            "SELECT s.run_id,s.scope_id,c.capability FROM analysis_scope s "
            "JOIN run_capability c ON c.run_id=s.run_id"
        )
    }
    stored_slices = list(
        database.execute(
            "SELECT slice_key,run_id,scope_id,capability,selected FROM slice_activation"
        )
    )
    stored_keys = {(row[0], row[1], row[2], row[3]) for row in stored_slices}
    selected_keys = {
        (row[0], row[1], row[2], row[3]) for row in stored_slices if row[4] == 1
    }
    if stored_keys != required_slices or selected_keys != required_slices:
        raise ValueError("selected slices do not exactly cover deterministic slice keys")

    subjects = {
        "symbol_claim_id": {row[0] for row in database.execute("SELECT claim_id FROM symbol_claim")},
        "relationship_claim_id": {
            row[0] for row in database.execute("SELECT claim_id FROM relationship_claim")
        },
        "diagnostic_id": {
            row[0] for row in database.execute("SELECT diagnostic_id FROM diagnostic")
        },
    }
    for column, expected in subjects.items():
        actual = [
            row[0]
            for row in database.execute(
                f"SELECT {column} FROM validity WHERE {column} IS NOT NULL"
            )
        ]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError("validity must bind every claim subject exactly once")


def _validate_connection(
    database: sqlite3.Connection,
    *,
    schema: GraphSchema = GraphSchema.V2,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
    publication_generation_id: str | None = None,
    publication_expected_active: object = _UNSET,
    repository_scope: RepositoryScope | None = None,
    source_manifest_sha256: str | None = None,
) -> None:
    """Validate the closed Evidence Graph file format and all stored integrity invariants."""
    _validation_deadline(database, deadline, monotonic, cancelled)
    try:
        signature = _schema_signature(database)
        if not isinstance(schema, GraphSchema):
            raise TypeError("schema must be a GraphSchema")
        if signature != _expected_schema_signature(schema):
            raise ValueError(f"Evidence Graph sqlite_schema is not the exact {schema.value} contract")
        schema_rows = database.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        tables = {row["name"] for row in schema_rows if row["type"] == "table"}
        indexes = {row["name"] for row in schema_rows if row["type"] == "index"}
        other = {row["type"] for row in schema_rows if row["type"] not in {"table", "index"}}
        expected_tables = set(_TABLE_COLUMNS) | (_V3_TABLES if schema is GraphSchema.V3 else set())
        expected_indexes = _EXPLICIT_INDEXES | (_V3_INDEXES if schema is GraphSchema.V3 else set())
        if tables != expected_tables or indexes != expected_indexes or other:
            raise ValueError(f"Evidence Graph sqlite_schema is not the exact {schema.value} contract")
        for table, expected in _TABLE_COLUMNS.items():
            _check_build_stop(deadline, cancelled, monotonic)
            columns = tuple(row["name"] for row in database.execute(f"PRAGMA table_info({table})"))
            if columns != expected:
                raise ValueError(f"Evidence Graph table columns differ for {table}")
        for index, expected in _INDEX_COLUMNS.items():
            _check_build_stop(deadline, cancelled, monotonic)
            columns = tuple(row["name"] for row in database.execute(f"PRAGMA index_info({index})"))
            if columns != expected:
                raise ValueError(f"Evidence Graph index columns differ for {index}")
        expected_user_version = 2 if schema is GraphSchema.V2 else 3
        if database.execute("PRAGMA user_version").fetchone()[0] != expected_user_version:
            raise ValueError("Evidence Graph schema version does not match selection")
        if str(database.execute("PRAGMA journal_mode").fetchone()[0]).casefold() != "delete":
            raise ValueError("Evidence Graph must use rollback-journal DELETE mode")
        if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Evidence Graph foreign key integrity check failed")
        integrity = database.execute("PRAGMA integrity_check(1)").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise ValueError("Evidence Graph integrity check failed")

        for table in expected_tables:
            _check_build_stop(deadline, cancelled, monotonic)
            count = database.execute(
                f"SELECT COUNT(*) FROM (SELECT 1 FROM {table} LIMIT ?)",
                (MAX_VALIDATION_ROWS + 1,),
            ).fetchone()[0]
            if count > MAX_VALIDATION_ROWS:
                raise ValueError(f"Evidence Graph {table} row ceiling exceeded")

        invalid = database.execute(
            """
SELECT 1 FROM source
WHERE length(sha256) != 64 OR sha256 GLOB '*[^0-9a-f]*'
   OR size < 0 OR size > 17179869184
   OR relative_path = '' OR relative_path LIKE '/%' OR relative_path LIKE '%\\%'
   OR relative_path = '..' OR relative_path LIKE '../%' OR relative_path LIKE '%/../%'
   OR media_type = '' OR length(media_type) > 256
   OR (language IS NOT NULL AND (language = '' OR length(language) > 128))
   OR (git_oid IS NOT NULL AND (git_oid = '' OR length(git_oid) > 128))
UNION ALL
SELECT 1 FROM node
WHERE node_id = '' OR length(node_id) > 512 OR metadata_json IS NULL OR NOT json_valid(metadata_json)
UNION ALL
SELECT 1 FROM occurrence o JOIN source s USING(source_id)
WHERE o.byte_start < 0 OR o.byte_end < o.byte_start OR o.byte_end > s.size
   OR o.line_start < 1 OR o.line_end < o.line_start
UNION ALL
SELECT 1 FROM assertion
WHERE confidence NOT IN ('high','medium','low')
   OR authority NOT IN ('user','web','ai-derived','inferred')
   OR resolution != 'resolved'
   OR (target_node_id IS NULL) = (literal_json IS NULL)
   OR (literal_json IS NOT NULL AND NOT json_valid(literal_json))
UNION ALL
SELECT 1 FROM observation
WHERE reason NOT IN ('ambiguous_target','dynamic_dispatch','missing_dependency',
                     'parse_error','unresolved_reference','unsupported_semantics')
UNION ALL
SELECT 1 FROM evidence e JOIN source s USING(source_id)
WHERE e.byte_start < 0 OR e.byte_end <= e.byte_start OR e.byte_end > s.size
   OR length(e.span_sha256) != 64 OR e.span_sha256 GLOB '*[^0-9a-f]*'
   OR (e.assertion_id IS NULL) = (e.observation_id IS NULL)
UNION ALL
SELECT 1 FROM assertion a
WHERE a.resolution='resolved'
  AND NOT EXISTS (SELECT 1 FROM evidence e WHERE e.assertion_id=a.assertion_id)
LIMIT 1
"""
        ).fetchone()
        if invalid is not None:
            raise ValueError(
                "Evidence Graph source, evidence, or controlled value integrity failed"
            )

        _validate_stored_records(
            database,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
        if schema is GraphSchema.V3:
            _validate_v3_sets(
                database,
                publication_generation_id=publication_generation_id,
                publication_expected_active=publication_expected_active,
                repository_scope=repository_scope,
                source_manifest_sha256=source_manifest_sha256,
            )
    except sqlite3.Error as exc:
        if bool(cancelled and cancelled()) or (
            deadline is not None and (monotonic() >= deadline or "interrupt" in str(exc).casefold())
        ):
            raise TimeoutError("Evidence Graph validation cancelled or deadline reached") from exc
        raise ValueError("Evidence Graph database validation failed") from exc
    finally:
        database.set_progress_handler(None, 0)


def _stored_shared_source_membership(
    database: sqlite3.Connection,
    *,
    deadline: float | None,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    _validation_deadline(database, deadline, monotonic, cancelled)
    try:
        rows = database.execute(
            "SELECT source_id, relative_path, sha256 FROM source "
            "ORDER BY relative_path, source_id LIMIT ?",
            (MAX_VALIDATION_ROWS + 1,),
        ).fetchall()
        if len(rows) > MAX_VALIDATION_ROWS:
            raise ValueError("Evidence Graph source row ceiling exceeded")
        _check_build_stop(deadline, cancelled, monotonic)
        return [
            {
                "relative_path": row["relative_path"],
                "sha256": row["sha256"],
                "logical_id": row["source_id"],
            }
            for row in rows
        ]
    except sqlite3.Error as exc:
        if bool(cancelled and cancelled()) or (
            deadline is not None and (monotonic() >= deadline or "interrupt" in str(exc).casefold())
        ):
            raise TimeoutError("Evidence Graph validation cancelled or deadline reached") from exc
        raise ValueError("Evidence Graph source manifest validation failed") from exc
    finally:
        database.set_progress_handler(None, 0)


def validate_generation_database(
    database_path: Path,
    *,
    schema: GraphSchema,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
    publication_generation_id: str | None = None,
    publication_expected_active: object = _UNSET,
    repository_scope: RepositoryScope | None = None,
    source_manifest_sha256: str | None = None,
) -> None:
    """Reopen and validate one exact, closed Evidence Graph database contract."""
    if not isinstance(schema, GraphSchema):
        raise TypeError("schema must be a GraphSchema")
    path = Path(database_path).resolve(strict=True)
    database = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0)
    try:
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        database.execute("PRAGMA trusted_schema=OFF")
        _validate_connection(
            database,
            schema=schema,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
            publication_generation_id=publication_generation_id,
            publication_expected_active=publication_expected_active,
            repository_scope=repository_scope,
            source_manifest_sha256=source_manifest_sha256,
        )
    finally:
        database.close()


def validate_generation_artifact(
    generation_path: Path,
    manifest: Mapping[str, object],
    *,
    state_root: Path,
    deadline: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Validate one graph artifact already bound by the shared generation manifest."""
    _check_build_stop(deadline, cancelled, monotonic)
    try:
        schema = GraphSchema(manifest.get("graph_schema_version"))
    except (TypeError, ValueError):
        raise ValueError("Evidence Graph manifest has the wrong graph schema version")
    graph_artifacts = [
        item for item in manifest.get("artifacts", []) if item.get("path") == "evidence.sqlite3"
    ]
    source_artifacts = [
        item for item in manifest.get("artifacts", []) if item.get("path") == "source-manifest.json"
    ]
    if len(graph_artifacts) != 1:
        raise ValueError("Evidence Graph manifest must bind exactly one evidence.sqlite3 artifact")
    if len(source_artifacts) != 1:
        raise ValueError(
            "Evidence Graph manifest must bind exactly one shared source-manifest.json artifact"
        )
    from corpus_snapshot import validate_canonical_source_manifest

    source_manifest_path = Path(generation_path) / "source-manifest.json"
    expected_source_manifest = validate_runtime_file(
        source_manifest_path, state_root, max_bytes=MAX_SOURCE_MANIFEST_BYTES
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source_manifest_path, flags)
    try:
        opened_source_manifest = os.fstat(descriptor)
        if not os.path.samestat(expected_source_manifest, opened_source_manifest):
            raise PermissionError("source manifest identity changed before read")
        source_chunks: list[bytes] = []
        source_manifest_size = 0
        while True:
            _check_build_stop(deadline, cancelled, monotonic)
            chunk = os.read(descriptor, IO_CHUNK_BYTES)
            if not chunk:
                break
            source_manifest_size += len(chunk)
            if source_manifest_size > MAX_SOURCE_MANIFEST_BYTES:
                raise ValueError("source manifest exceeds the supported byte ceiling")
            source_chunks.append(chunk)
        source_manifest_after = os.fstat(descriptor)
        if not os.path.samestat(opened_source_manifest, source_manifest_after) or (
            opened_source_manifest.st_size,
            opened_source_manifest.st_mtime_ns,
        ) != (source_manifest_after.st_size, source_manifest_after.st_mtime_ns):
            raise PermissionError("source manifest changed during bounded read")
        source_manifest_bytes = b"".join(source_chunks)
    finally:
        os.close(descriptor)
    _check_build_stop(deadline, cancelled, monotonic)
    try:
        source_manifest_value = json.loads(source_manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("shared source manifest must contain valid UTF-8 JSON") from exc
    source_manifest = validate_canonical_source_manifest(source_manifest_value)
    if canonical_json_bytes(source_manifest) != source_manifest_bytes:
        raise ValueError("shared source manifest artifact must use canonical JSON")
    source_manifest_hash = hashlib.sha256(source_manifest_bytes).hexdigest()
    if source_manifest_hash != manifest.get("source_manifest_sha256"):
        raise ValueError("shared source manifest hash does not match generation manifest")
    if source_manifest["collector"] != manifest.get("collector_version") or source_manifest[
        "extractor"
    ] != manifest.get("extractor_version"):
        raise ValueError("shared source manifest versions do not match generation manifest")
    artifact = Path(generation_path) / "evidence.sqlite3"
    expected = validate_runtime_file(artifact, state_root, max_bytes=MAX_DATABASE_BYTES)
    database = sqlite3.connect(
        f"{artifact.resolve(strict=True).as_uri()}?mode=ro", uri=True, timeout=0
    )
    try:
        current = artifact.stat(follow_symlinks=False)
        if not os.path.samestat(expected, current):
            raise PermissionError("Evidence Graph identity changed while validating")
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA query_only=ON")
        database.execute("PRAGMA trusted_schema=OFF")
        _validate_connection(
            database,
            schema=schema,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
            publication_generation_id=(
                manifest.get("generation_id") if schema is GraphSchema.V3 else None
            ),
            repository_scope=(
                RepositoryScope.from_dict(manifest.get("repository_scope"))
                if schema is GraphSchema.V3
                else None
            ),
            source_manifest_sha256=(
                manifest.get("source_manifest_sha256") if schema is GraphSchema.V3 else None
            ),
        )
        stored_sources = _stored_shared_source_membership(
            database,
            deadline=deadline,
            monotonic=monotonic,
            cancelled=cancelled,
        )
        if stored_sources != source_manifest["sources"]:
            raise ValueError("shared source manifest does not match graph source rows")
        _check_build_stop(deadline, cancelled, monotonic)
    finally:
        database.close()


class EvidenceGraph:
    """Read-only facade over one catalog-selected immutable graph generation."""

    def __init__(
        self,
        database_path: Path,
        *,
        state_root: Path,
        generation_id: str | None = None,
        schema: GraphSchema = GraphSchema.V2,
        max_database_bytes: int = MAX_DATABASE_BYTES,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        _check_build_stop(deadline, cancelled, time.monotonic)
        self.database_path = Path(database_path)
        self.state_root = Path(state_root)
        self.generation_id = generation_id
        if not isinstance(schema, GraphSchema):
            raise TypeError("schema must be a GraphSchema")
        self.schema = schema
        try:
            expected = validate_runtime_file(
                self.database_path, self.state_root, max_bytes=max_database_bytes
            )
        except FileNotFoundError as exc:
            raise PermissionError(
                "Evidence Graph must remain inside an existing state root"
            ) from exc
        uri = f"{self.database_path.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        database = sqlite3.connect(uri, uri=True, timeout=0)
        try:
            current = self.database_path.stat(follow_symlinks=False)
            if not os.path.samestat(expected, current):
                raise PermissionError("Evidence Graph identity changed while opening")
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA query_only=ON")
            database.execute("PRAGMA trusted_schema=OFF")
            _validate_connection(
                database, schema=schema, deadline=deadline, cancelled=cancelled
            )
            self._database = database
        except BaseException:
            database.close()
            raise

    @classmethod
    def open_active(
        cls,
        catalog: object,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> EvidenceGraph | None:
        options = {}
        if deadline is not None:
            options["deadline"] = deadline
        if cancelled is not None:
            options["cancelled"] = cancelled
        for _attempt in range(3):
            manifest = catalog.get_active(**options)
            if manifest is None:
                return None
            try:
                schema = GraphSchema(manifest.get("graph_schema_version"))
            except (TypeError, ValueError):
                raise ValueError("active generation does not use the Evidence Graph schema")
            generation_id = manifest["generation_id"]
            artifacts = {item["path"] for item in manifest["artifacts"]}
            if "evidence.sqlite3" not in artifacts:
                raise ValueError("active graph generation has no evidence.sqlite3 artifact")
            graph = None
            try:
                validated, seal = catalog._registered_generation(generation_id, **options)
                if validated != manifest:
                    continue
                generation_path = catalog.generations_path / generation_id
                graph = cls(
                    generation_path / "evidence.sqlite3",
                    state_root=catalog.state_root,
                    generation_id=generation_id,
                    schema=schema,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                if not catalog._deadline_seal_unchanged(
                    generation_path, seal, deadline, cancelled=cancelled
                ):
                    graph.close()
                    graph = None
                    continue
                if catalog.get_active(**options) != manifest:
                    graph.close()
                    graph = None
                    continue
                return graph
            except (FileNotFoundError, PermissionError, TypeError, ValueError, sqlite3.Error):
                if graph is not None:
                    graph.close()
                continue
        raise PermissionError("active Evidence Graph changed while opening")

    @classmethod
    def open_active_for_repository(
        cls,
        catalog: object,
        repository_scope: RepositoryScope,
        *,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> EvidenceGraph | None:
        """Open only a generation bound to the exact requested repository state."""
        _check_build_stop(deadline, cancelled, time.monotonic)
        expected_scope = RepositoryScope.from_dict(repository_scope.as_dict())
        options = {}
        if deadline is not None:
            options["deadline"] = deadline
        if cancelled is not None:
            options["cancelled"] = cancelled
        for _attempt in range(3):
            _check_build_stop(deadline, cancelled, time.monotonic)
            manifest = catalog.get_active_for_repository(expected_scope, **options)
            if manifest is None:
                return None
            try:
                schema = GraphSchema(manifest.get("graph_schema_version"))
            except (TypeError, ValueError):
                return None
            generation_id = manifest["generation_id"]
            graph = None
            try:
                validated, seal = catalog._registered_generation(generation_id, **options)
                generation_scope = RepositoryScope.from_dict(validated.get("repository_scope"))
                if validated != manifest or generation_scope != expected_scope:
                    continue
                artifacts = {item["path"] for item in validated["artifacts"]}
                if "evidence.sqlite3" not in artifacts:
                    return None
                generation_path = catalog.generations_path / generation_id
                graph = cls(
                    generation_path / "evidence.sqlite3",
                    state_root=catalog.state_root,
                    generation_id=generation_id,
                    schema=schema,
                    deadline=deadline,
                    cancelled=cancelled,
                )
                graph.repository_scope = generation_scope
                if not catalog._deadline_seal_unchanged(
                    generation_path, seal, deadline, cancelled=cancelled
                ):
                    graph.close()
                    graph = None
                    continue
                if catalog.get_active_for_repository(expected_scope, **options) != manifest:
                    graph.close()
                    graph = None
                    continue
                return graph
            except (FileNotFoundError, PermissionError, TypeError, ValueError, sqlite3.Error):
                if graph is not None:
                    graph.close()
                return None
        raise PermissionError("active Evidence Graph changed while opening")

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
            if deadline is not None and (
                time.monotonic() >= deadline or "interrupt" in str(exc).lower()
            ):
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
        identifier = _node_id(node_id)
        row = self._database.execute(
            "SELECT node_id, kind, identity_scheme, identity_key, metadata_json "
            "FROM node WHERE node_id = ?",
            (identifier,),
        ).fetchone()
        return None if row is None else self._node(row)

    def find_nodes(
        self,
        *,
        kinds: Sequence[str] | None = None,
        name: str | None = None,
        path: str | None = None,
        max_rows: int = 100,
        deadline: float | None = None,
    ) -> list[dict[str, object]]:
        """Find bounded nodes by indexed kind and exact metadata fields."""
        clauses = []
        parameters: list[object] = []
        if kinds is not None:
            values = tuple(sorted({_text(kind, "node kind", maximum=128) for kind in kinds}))
            if not values or len(values) > MAX_EDGE_TYPES:
                raise ValueError("kinds must contain between 1 and 64 values")
            clauses.append(f"kind IN ({','.join('?' for _ in values)})")
            parameters.extend(values)
        if name is not None:
            clauses.append("json_extract(metadata_json, '$.name') = ?")
            parameters.append(_text(name, "node name", maximum=1024))
        if path is not None:
            clauses.append("json_extract(metadata_json, '$.path') = ?")
            parameters.append(_text(path, "node path", maximum=4096))
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        rows = self._execute(
            "SELECT node_id, kind, identity_scheme, identity_key, metadata_json "
            f"FROM node{where} ORDER BY kind, identity_key, node_id LIMIT ?",
            parameters,
            max_rows=max_rows,
            deadline=deadline,
        )
        return [self._node(row) for row in rows]

    def edges(
        self,
        *,
        edge_types: Sequence[str] | None = None,
        max_rows: int = 100,
        deadline: float | None = None,
    ) -> list[dict[str, object]]:
        """Return bounded resolved node-to-node assertions for store facades."""
        values = _edge_type_values(edge_types)
        filter_sql = ""
        parameters: list[object] = []
        if values:
            filter_sql = f" AND edge_type IN ({','.join('?' for _ in values)})"
            parameters.extend(values)
        rows = self._execute(
            "SELECT assertion_id, source_node_id, edge_type, target_node_id, "
            "confidence, authority, resolution, extractor FROM assertion "
            "WHERE resolution='resolved' AND target_node_id IS NOT NULL"
            f"{filter_sql} ORDER BY edge_type, source_node_id, target_node_id, assertion_id LIMIT ?",
            parameters,
            max_rows=max_rows,
            deadline=deadline,
        )
        return [dict(row) for row in rows]

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
        max_work: int,
        deadline: float | None,
    ) -> list[dict[str, object]]:
        if direction not in {"in", "out"}:
            raise ValueError("direction must be in or out")
        depth = _bound(max_depth, "max_depth", MAX_DEPTH)
        edge_values = _edge_type_values(edge_types)
        work_limit = _bound(max_work, "max_work", MAX_WORK)
        filter_sql = ""
        parameters: list[object] = [_node_id(node_id), depth]
        if edge_values:
            filter_sql = f" AND a.edge_type IN ({','.join('?' for _ in edge_values)})"
            parameters.extend(edge_values)
        source_column, target_column = (
            ("source_node_id", "target_node_id")
            if direction == "out"
            else ("target_node_id", "source_node_id")
        )
        sql = f"""
WITH RECURSIVE walk(node_id, depth, seen, edge_order) AS (
  SELECT ?, 0, ',' || ? || ',', ''
  UNION ALL
  SELECT a.{target_column}, walk.depth + 1, walk.seen || a.{target_column} || ',', a.assertion_id
  FROM walk JOIN assertion a ON a.{source_column} = walk.node_id
  WHERE walk.depth < ? AND a.resolution = 'resolved' AND a.target_node_id IS NOT NULL
    AND instr(walk.seen, ',' || a.{target_column} || ',') = 0{filter_sql}
  ORDER BY 2, 1, 4
  LIMIT ?
)
SELECT n.node_id, n.kind, n.identity_scheme, n.identity_key, n.metadata_json,
       min(w.depth) AS depth, (SELECT count(*) FROM walk) AS work_count
FROM walk w JOIN node n USING(node_id) WHERE w.depth > 0
GROUP BY n.node_id ORDER BY depth, n.kind, n.identity_key, n.node_id LIMIT ?
"""
        parameters.insert(1, parameters[0])
        parameters.append(work_limit + 2)
        rows = self._execute(sql, parameters, max_rows=max_rows, deadline=deadline)
        if rows and rows[0]["work_count"] - 1 > work_limit:
            raise ValueError("Evidence Graph recursive work ceiling exceeded")
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
        max_work: int = 1000,
        deadline: float | None = None,
    ):
        return self._traverse(
            node_id,
            direction=direction,
            edge_types=edge_types,
            max_depth=max_depth,
            max_rows=max_rows,
            max_work=max_work,
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
        max_work: int = 1000,
        deadline: float | None = None,
    ):
        if not isinstance(reverse, bool):
            raise ValueError("reverse must be boolean")
        depth = _bound(max_depth, "max_depth", MAX_DEPTH)
        work_limit = _bound(max_work, "max_work", MAX_WORK)
        start, target = (
            ("dependency_node_id", "dependent_node_id")
            if reverse
            else ("dependent_node_id", "dependency_node_id")
        )
        rows = self._execute(
            f"""
WITH RECURSIVE walk(node_id, depth, seen, edge_order) AS (
  SELECT ?, 0, ',' || ? || ',', ''
  UNION ALL
  SELECT d.{target}, w.depth + 1, w.seen || d.{target} || ',', d.dependency_id
  FROM walk w JOIN dependency d ON d.{start}=w.node_id
  WHERE w.depth < ? AND instr(w.seen, ',' || d.{target} || ',') = 0
  ORDER BY 2, 1, 4
  LIMIT ?
)
SELECT n.node_id, n.kind, n.identity_scheme, n.identity_key, n.metadata_json,
       min(w.depth) AS depth, (SELECT count(*) FROM walk) AS work_count
FROM walk w JOIN node n USING(node_id) WHERE w.depth > 0
GROUP BY n.node_id ORDER BY depth, n.kind, n.identity_key, n.node_id LIMIT ?
""",
            (
                _node_id(node_id),
                _node_id(node_id),
                depth,
                work_limit + 2,
            ),
            max_rows=max_rows,
            deadline=deadline,
        )
        if rows and rows[0]["work_count"] - 1 > work_limit:
            raise ValueError("Evidence Graph recursive work ceiling exceeded")
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
        max_work: int = 1000,
        deadline: float | None = None,
    ):
        depth = _bound(max_depth, "max_depth", MAX_DEPTH)
        work_limit = _bound(max_work, "max_work", MAX_WORK)
        source_id = _node_id(source_node_id, "source_node_id")
        target_id = _node_id(target_node_id, "target_node_id")
        rows = self._execute(
            """
WITH RECURSIVE walk(node_id, depth, node_ids, assertion_ids, seen, edge_order) AS (
  SELECT ?, 0, json_array(?), json_array(), ',' || ? || ',', ''
  UNION ALL
  SELECT a.target_node_id, w.depth + 1,
         json_insert(w.node_ids, '$[#]', a.target_node_id),
         json_insert(w.assertion_ids, '$[#]', a.assertion_id),
         w.seen || a.target_node_id || ',', a.assertion_id
  FROM walk w JOIN assertion a ON a.source_node_id = w.node_id
  WHERE w.depth < ? AND a.resolution='resolved' AND a.target_node_id IS NOT NULL
    AND instr(w.seen, ',' || a.target_node_id || ',') = 0
  ORDER BY 2, 1, 6
  LIMIT ?
)
SELECT node_ids, assertion_ids, depth, (SELECT count(*) FROM walk) AS work_count
FROM walk WHERE node_id=? AND depth > 0
ORDER BY depth, assertion_ids LIMIT ?
""",
            (
                source_id,
                source_id,
                source_id,
                depth,
                work_limit + 2,
                target_id,
            ),
            max_rows=max_rows,
            deadline=deadline,
        )
        if rows and rows[0]["work_count"] - 1 > work_limit:
            raise ValueError("Evidence Graph recursive work ceiling exceeded")
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
            f"SELECT e.*, s.relative_path, s.sha256 AS source_sha256, s.content AS source_content FROM evidence e "
            f"JOIN source s USING(source_id) WHERE e.{column}=? "
            "ORDER BY s.relative_path, e.byte_start, e.evidence_id LIMIT ?",
            (_text(value, column, maximum=512),),
            max_rows=max_rows,
            deadline=deadline,
        )
        result = []
        for row in rows:
            item = dict(row)
            content = item.pop("source_content")
            item["line_start"] = content.count(b"\n", 0, item["byte_start"]) + 1
            item["line_end"] = content.count(b"\n", 0, item["byte_end"]) + 1
            result.append(item)
        return result

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
