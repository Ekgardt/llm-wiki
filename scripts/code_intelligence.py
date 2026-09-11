"""Immutable normalized contracts for code-intelligence analysis."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, unique

from corpus_snapshot import CorpusSnapshot, canonical_source_manifest_sha256
from reliable_memory import canonical_json_bytes

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GENERATED_SOURCES = frozenset({"available", "unavailable", "not-required"})
_DEPENDENCY_RESOLUTION = frozenset({"complete", "partial", "unavailable"})
_ANALYZER_SUPPORT = frozenset({"complete", "partial", "unsupported", "unqualified"})
_ANALYSIS_MODES = frozenset({"precise", "native-syntax"})
_PROTOCOLS = frozenset({"scip", "lsp", "native"})
_SOURCE_DISPOSITIONS = frozenset({"included", "excluded", "generated"})
_SQLITE_INT64_MAX = 2**63 - 1


@unique
class Capability(str, Enum):
    DEFINITIONS = "definitions"
    DECLARATIONS = "declarations"
    REFERENCES = "references"
    CALLS = "calls"
    IMPORTS = "imports"
    TYPES = "types"
    TYPE_DEFINITIONS = "type_definitions"
    INHERITANCE = "inheritance"
    IMPLEMENTATIONS = "implementations"
    DIAGNOSTICS = "diagnostics"


@unique
class PositionEncoding(str, Enum):
    UTF8 = "utf-8"
    UTF16 = "utf-16"
    UTF32 = "utf-32"


@unique
class CoverageStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    EXCLUDED = "excluded"


@unique
class EvidenceLevel(str, Enum):
    COMPILER = "compiler"
    SEMANTIC = "semantic"
    SYNTAX = "syntax"
    LEXICAL = "lexical"


@unique
class AnalysisOutcome(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@unique
class ValidityStatus(str, Enum):
    CURRENT = "current"
    SOFT_STALE = "soft-stale"
    HARD_STALE = "hard-stale"


@unique
class SubjectKind(str, Enum):
    SYMBOL = "symbol"
    RELATIONSHIP = "relationship"
    DIAGNOSTIC = "diagnostic"


@unique
class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    HINT = "hint"


@unique
class Relationship(str, Enum):
    REFERENCES_SYMBOL = "REFERENCES_SYMBOL"
    CALLS = "CALLS"
    IMPORTS = "IMPORTS"
    HAS_TYPE = "HAS_TYPE"
    TYPE_DEFINITION = "TYPE_DEFINITION"
    INHERITS = "INHERITS"
    IMPLEMENTS = "IMPLEMENTS"


@unique
class RelationshipResolution(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@unique
class SymbolRole(str, Enum):
    DEFINITION = "definition"
    DECLARATION = "declaration"


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} sha256 must be a string")
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} sha256 must be 64 lowercase hexadecimal characters")
    return value


def _require_text(value: object, label: str, *, maximum: int, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    _require_bounded_utf8(value, label, maximum)
    return value


def _require_bounded_utf8(value: str, label: str, maximum: int) -> None:
    encoded = _utf8(value, label)
    if not encoded:
        raise ValueError(f"{label} must not be empty")
    if len(encoded) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    _require_nfc(value, label)


def _utf8(value: str, label: str) -> bytes:
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc


def _require_nfc(value: str, label: str) -> None:
    if not unicodedata.is_normalized("NFC", value):
        raise ValueError(f"{label} must use NFC normalization")


def _require_record(value: object, kind: type, message: str) -> None:
    if not isinstance(value, kind):
        raise TypeError(message)


def _require_records(values: tuple, kind: type, message: str) -> None:
    if any(not isinstance(item, kind) for item in values):
        raise TypeError(message)


def _require_typed_tuple(values: object, label: str, kind: type) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be a tuple")
    _require_records(values, kind, f"{label} contains an invalid record type")


def _require_all(checks: Iterable[tuple[Callable[[], bool], Exception]]) -> None:
    """Raise the error of the first check, in order, that does not hold."""
    for holds, error in checks:
        if not holds():
            raise error


def _require_reason_presence(
    settled: bool, reason: object, forbidden: str, required: str
) -> None:
    """A settled state carries no reason; an unsettled one must name it."""
    if settled and reason is not None:
        raise ValueError(forbidden)
    if not settled and reason is None:
        raise ValueError(required)


def _require_id(value: object, label: str) -> str:
    result = _require_text(value, label, maximum=512)
    assert result is not None
    if any(character.isspace() or unicodedata.category(character).startswith("C") for character in result):
        raise ValueError(f"{label} contains whitespace or control characters")
    return result


def _require_enum(value: object, enum_type: type[Enum], label: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{label} must be {enum_type.__name__}")


def _require_bool(value: object, label: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{label} must be bool")


def _require_sqlite_int(value: object, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    _require_int64_range(value, label, minimum)
    return value


def _require_int64_range(value: int, label: str, minimum: int) -> None:
    if value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if value > _SQLITE_INT64_MAX:
        raise ValueError(f"{label} must fit a signed int64")


def _require_choice(value: object, choices: frozenset[str], label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if value not in choices:
        raise ValueError(f"invalid {label}: {value!r}")


def _require_sorted_unique(values: object, label: str, *, key) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be a tuple")
    _require_sorted_unique_keys(tuple(key(item) for item in values), label)


def _require_sorted_unique_keys(keys: tuple, label: str) -> None:
    if keys != tuple(sorted(keys)):
        raise ValueError(f"{label} must be sorted")
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must be unique")


@dataclass(frozen=True, slots=True)
class AnalysisIdentity:
    source_manifest_sha256: str
    manifest_sha256: str
    lockfile_sha256: str
    sdk_sha256: str
    target_sha256: str
    configuration_sha256: str
    feature_sha256: str
    invocation_sha256: str
    environment_sha256: str
    dependency_state_sha256: str
    position_encoding: PositionEncoding
    analysis_sha256: str

    def __post_init__(self) -> None:
        for name in self._component_names():
            _require_sha256(getattr(self, name), name)
        _require_enum(self.position_encoding, PositionEncoding, "position_encoding")
        _require_sha256(self.analysis_sha256, "analysis_sha256")

    @staticmethod
    def _component_names() -> tuple[str, ...]:
        return (
            "source_manifest_sha256",
            "manifest_sha256",
            "lockfile_sha256",
            "sdk_sha256",
            "target_sha256",
            "configuration_sha256",
            "feature_sha256",
            "invocation_sha256",
            "environment_sha256",
            "dependency_state_sha256",
        )

    @classmethod
    def create(
        cls,
        *,
        source_manifest_sha256: str,
        manifest_sha256: str,
        lockfile_sha256: str,
        sdk_sha256: str,
        target_sha256: str,
        configuration_sha256: str,
        feature_sha256: str,
        invocation_sha256: str,
        environment_sha256: str,
        dependency_state_sha256: str,
        position_encoding: PositionEncoding,
    ) -> AnalysisIdentity:
        inputs = {
            "source_manifest_sha256": source_manifest_sha256,
            "manifest_sha256": manifest_sha256,
            "lockfile_sha256": lockfile_sha256,
            "sdk_sha256": sdk_sha256,
            "target_sha256": target_sha256,
            "configuration_sha256": configuration_sha256,
            "feature_sha256": feature_sha256,
            "invocation_sha256": invocation_sha256,
            "environment_sha256": environment_sha256,
            "dependency_state_sha256": dependency_state_sha256,
        }
        for name, value in inputs.items():
            _require_sha256(value, name)
        _require_enum(position_encoding, PositionEncoding, "position_encoding")
        digest_inputs = {**inputs, "position_encoding": position_encoding.value}
        analysis_sha256 = hashlib.sha256(canonical_json_bytes(digest_inputs)).hexdigest()
        return cls(**inputs, position_encoding=position_encoding, analysis_sha256=analysis_sha256)

    def recompute_analysis_sha256(self) -> str:
        values = {name: getattr(self, name) for name in self._component_names()}
        values["position_encoding"] = self.position_encoding.value
        return hashlib.sha256(canonical_json_bytes(values)).hexdigest()

    def as_dict(self) -> dict[str, str]:
        return {
            "source_manifest_sha256": self.source_manifest_sha256,
            "manifest_sha256": self.manifest_sha256,
            "lockfile_sha256": self.lockfile_sha256,
            "sdk_sha256": self.sdk_sha256,
            "target_sha256": self.target_sha256,
            "configuration_sha256": self.configuration_sha256,
            "feature_sha256": self.feature_sha256,
            "invocation_sha256": self.invocation_sha256,
            "environment_sha256": self.environment_sha256,
            "dependency_state_sha256": self.dependency_state_sha256,
            "position_encoding": self.position_encoding.value,
            "analysis_sha256": self.analysis_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExpectedSource:
    source_id: str
    source_sha256: str
    disposition: str

    def __post_init__(self) -> None:
        _require_id(self.source_id, "source_id")
        _require_sha256(self.source_sha256, "source_sha256")
        _require_choice(self.disposition, _SOURCE_DISPOSITIONS, "disposition")


@dataclass(frozen=True, slots=True)
class AnalysisScope:
    scope_id: str
    run_id: str
    source_manifest_sha256: str
    build_target: str
    build_configuration: str
    expected_sources: tuple[ExpectedSource, ...]
    generated_sources: str
    dependency_resolution: str
    analyzer_support: str

    def __post_init__(self) -> None:
        _require_id(self.scope_id, "scope_id")
        _require_id(self.run_id, "run_id")
        _require_sha256(self.source_manifest_sha256, "source_manifest_sha256")
        _require_text(self.build_target, "build_target", maximum=256)
        _require_text(self.build_configuration, "build_configuration", maximum=256)
        if not isinstance(self.expected_sources, tuple):
            raise TypeError("expected_sources must be a tuple")
        if any(not isinstance(item, ExpectedSource) for item in self.expected_sources):
            raise TypeError("expected_sources must contain ExpectedSource records")
        _require_sorted_unique(
            self.expected_sources,
            "expected_sources",
            key=lambda item: item.source_id,
        )
        _require_choice(self.generated_sources, _GENERATED_SOURCES, "generated_sources")
        _require_choice(self.dependency_resolution, _DEPENDENCY_RESOLUTION, "dependency_resolution")
        _require_choice(self.analyzer_support, _ANALYZER_SUPPORT, "analyzer_support")

    @property
    def expected_source_ids(self) -> tuple[str, ...]:
        return tuple(item.source_id for item in self.expected_sources)


@dataclass(frozen=True, slots=True)
class PositionRange:
    byte_start: int
    byte_end: int

    def __post_init__(self) -> None:
        for name in ("byte_start", "byte_end"):
            _require_sqlite_int(getattr(self, name), name, minimum=0)
        if self.byte_end < self.byte_start:
            raise ValueError("byte_end must not precede byte_start")

    def require_nonempty(self, label: str) -> PositionRange:
        _require_text(label, "range label", maximum=128)
        if self.byte_end <= self.byte_start:
            raise ValueError(f"{label} must use a non-empty half-open byte range")
        return self


@dataclass(frozen=True, slots=True)
class Coverage:
    scope_id: str
    source_id: str
    capability: Capability
    status: CoverageStatus
    closed_world_eligible: bool
    reason: str | None

    def __post_init__(self) -> None:
        _require_id(self.scope_id, "scope_id")
        _require_id(self.source_id, "source_id")
        _require_enum(self.capability, Capability, "capability")
        _require_enum(self.status, CoverageStatus, "status")
        _require_bool(self.closed_world_eligible, "closed_world_eligible")
        terminal = self.status in {CoverageStatus.COMPLETE, CoverageStatus.EXCLUDED}
        _require_reason_presence(
            terminal,
            self.reason,
            "complete or excluded coverage must not have a reason",
            "incomplete coverage must have a reason",
        )
        _require_text(self.reason, "coverage reason", maximum=1024, optional=True)
        if self.closed_world_eligible and not terminal:
            raise ValueError("closed-world eligible coverage must be complete or excluded")


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    run_id: str
    identity: AnalysisIdentity
    source_manifest_sha256: str
    analysis_mode: str
    repository_id: str
    checkout_id: str
    source_generation_id: str
    analyzer_family: str
    analyzer_version: str
    protocol: str
    protocol_version: str
    executable_sha256: str
    declared_capabilities: tuple[Capability, ...]
    evidence_level: EvidenceLevel
    qualified: bool
    outcome: AnalysisOutcome
    receipt_sha256: str | None
    receipt_output_sha256: str | None
    consent_grant_id: str | None
    consent_revision: int | None
    lease_id: str | None
    started_at: str
    ended_at: str

    def __post_init__(self) -> None:
        _require_id(self.run_id, "run_id")
        _require_identity(self.identity)
        _require_sha256(self.source_manifest_sha256, "source_manifest_sha256")
        if self.source_manifest_sha256 != self.identity.source_manifest_sha256:
            raise ValueError("run source_manifest_sha256 must match identity")
        _require_choice(self.analysis_mode, _ANALYSIS_MODES, "analysis_mode")
        for name in ("repository_id", "checkout_id", "source_generation_id"):
            _require_id(getattr(self, name), name)
        for name in ("analyzer_family", "analyzer_version", "protocol_version"):
            _require_text(getattr(self, name), name, maximum=256)
        _require_protocol(self.protocol)
        _require_sha256(self.executable_sha256, "executable_sha256")
        _require_declared_capabilities(self.declared_capabilities)
        _require_enum(self.evidence_level, EvidenceLevel, "evidence_level")
        _require_bool(self.qualified, "qualified")
        _require_enum(self.outcome, AnalysisOutcome, "outcome")
        _require_run_times(self.started_at, self.ended_at)
        self._validate_mode()

    def _validate_mode(self) -> None:
        consent = (self.receipt_sha256, self.receipt_output_sha256, self.consent_grant_id, self.lease_id)
        if self.analysis_mode == "precise":
            self._validate_precise(consent)
            return
        self._validate_native(consent)

    def _validate_native(self, consent: tuple[str | None, ...]) -> None:
        if self.evidence_level not in {EvidenceLevel.SYNTAX, EvidenceLevel.LEXICAL}:
            raise ValueError("native-syntax analysis requires syntax or lexical evidence")
        if any(item is not None for item in consent) or self.consent_revision is not None:
            raise ValueError("native-syntax analysis must not contain receipt, consent, or lease")

    def _validate_precise(self, consent: tuple[str | None, ...]) -> None:
        if self.evidence_level not in {EvidenceLevel.COMPILER, EvidenceLevel.SEMANTIC}:
            raise ValueError("precise analysis requires compiler or semantic evidence")
        if any(item is None for item in consent) or self.consent_revision is None:
            raise ValueError("precise analysis requires receipt, consent grant, revision, and lease")
        _require_sha256(self.receipt_sha256, "receipt_sha256")
        _require_sha256(self.receipt_output_sha256, "receipt_output_sha256")
        _require_id(self.consent_grant_id, "consent_grant_id")
        _require_id(self.lease_id, "lease_id")
        _require_sqlite_int(self.consent_revision, "consent_revision", minimum=1)


@dataclass(frozen=True, slots=True)
class SymbolIdentity:
    scheme: str
    value: str

    def __post_init__(self) -> None:
        _require_text(self.scheme, "identity scheme", maximum=64)
        _require_text(self.value, "identity value", maximum=2048)


@dataclass(frozen=True, slots=True)
class SymbolClaim:
    claim_id: str
    run_id: str
    scope_id: str
    source_id: str
    capability: Capability
    identity: SymbolIdentity
    display_name: str
    symbol_kind: str
    role: SymbolRole
    range: PositionRange
    evidence_level: EvidenceLevel
    ambiguity: bool

    def __post_init__(self) -> None:
        for name in ("claim_id", "run_id", "scope_id", "source_id"):
            _require_id(getattr(self, name), name)
        _require_enum(self.capability, Capability, "capability")
        _require_record(self.identity, SymbolIdentity, "identity must be SymbolIdentity")
        _require_text(self.display_name, "display_name", maximum=1024)
        _require_text(self.symbol_kind, "symbol_kind", maximum=128)
        _require_enum(self.role, SymbolRole, "role")
        required = {
            SymbolRole.DEFINITION: Capability.DEFINITIONS,
            SymbolRole.DECLARATION: Capability.DECLARATIONS,
        }[self.role]
        if self.capability is not required:
            raise ValueError("symbol capability must agree with role")
        _require_record(self.range, PositionRange, "range must be PositionRange")
        self.range.require_nonempty("symbol claim range")
        _require_enum(self.evidence_level, EvidenceLevel, "evidence_level")
        _require_bool(self.ambiguity, "ambiguity")


_RELATION_CAPABILITY = {
    Relationship.REFERENCES_SYMBOL: Capability.REFERENCES,
    Relationship.CALLS: Capability.CALLS,
    Relationship.IMPORTS: Capability.IMPORTS,
    Relationship.HAS_TYPE: Capability.TYPES,
    Relationship.TYPE_DEFINITION: Capability.TYPE_DEFINITIONS,
    Relationship.INHERITS: Capability.INHERITANCE,
    Relationship.IMPLEMENTS: Capability.IMPLEMENTATIONS,
}


@dataclass(frozen=True, slots=True)
class RelationshipClaim:
    claim_id: str
    run_id: str
    scope_id: str
    source_id: str
    source_identity: SymbolIdentity
    relation: Relationship
    capability: Capability
    target_identity: SymbolIdentity | None
    target_text: str | None
    resolution: RelationshipResolution
    range: PositionRange
    evidence_level: EvidenceLevel
    ambiguity: bool

    def __post_init__(self) -> None:
        for name in ("claim_id", "run_id", "scope_id", "source_id"):
            _require_id(getattr(self, name), name)
        _require_record(
            self.source_identity, SymbolIdentity, "source_identity must be SymbolIdentity"
        )
        _require_enum(self.relation, Relationship, "relation")
        _require_enum(self.capability, Capability, "capability")
        if self.capability is not _RELATION_CAPABILITY[self.relation]:
            raise ValueError("relationship capability must agree with relation")
        _require_enum(self.resolution, RelationshipResolution, "resolution")
        _require_relationship_target(self.resolution, self.target_identity, self.target_text)
        _require_record(self.range, PositionRange, "range must be PositionRange")
        self.range.require_nonempty("relationship claim range")
        _require_enum(self.evidence_level, EvidenceLevel, "evidence_level")
        _require_bool(self.ambiguity, "ambiguity")
        if self.ambiguity is not (self.resolution is RelationshipResolution.AMBIGUOUS):
            raise ValueError("ambiguity must be true exactly for ambiguous resolution")


@dataclass(frozen=True, slots=True)
class RelatedLocation:
    source_id: str
    range: PositionRange
    message: str | None

    def __post_init__(self) -> None:
        _require_id(self.source_id, "source_id")
        if not isinstance(self.range, PositionRange):
            raise TypeError("range must be PositionRange")
        self.range.require_nonempty("related location range")
        if self.message is not None:
            _require_text(self.message, "related message", maximum=4096)


@dataclass(frozen=True, slots=True)
class Diagnostic:
    diagnostic_id: str
    run_id: str
    scope_id: str
    source_id: str
    capability: Capability
    severity: DiagnosticSeverity
    code: str | None
    message: str
    range: PositionRange
    evidence_level: EvidenceLevel
    related: tuple[RelatedLocation, ...]

    def __post_init__(self) -> None:
        for name in ("diagnostic_id", "run_id", "scope_id", "source_id"):
            _require_id(getattr(self, name), name)
        _require_enum(self.capability, Capability, "capability")
        if self.capability is not Capability.DIAGNOSTICS:
            raise ValueError("diagnostic capability must be diagnostics")
        _require_enum(self.severity, DiagnosticSeverity, "severity")
        _require_text(self.code, "diagnostic code", maximum=256, optional=True)
        _require_text(self.message, "diagnostic message", maximum=8192)
        _require_record(self.range, PositionRange, "range must be PositionRange")
        self.range.require_nonempty("diagnostic range")
        _require_enum(self.evidence_level, EvidenceLevel, "evidence_level")
        if self.evidence_level is EvidenceLevel.LEXICAL:
            raise ValueError("diagnostic evidence cannot be lexical")
        _require_sorted_unique(
            self.related,
            "diagnostic related locations",
            key=_related_location_key,
        )
        _require_records(
            self.related,
            RelatedLocation,
            "diagnostic related locations must contain RelatedLocation",
        )


@dataclass(frozen=True, slots=True)
class Validity:
    validity_id: str
    subject_kind: SubjectKind
    subject_id: str
    status: ValidityStatus
    stale_reason: str | None

    def __post_init__(self) -> None:
        _require_id(self.validity_id, "validity_id")
        _require_enum(self.subject_kind, SubjectKind, "subject_kind")
        _require_id(self.subject_id, "subject_id")
        _require_enum(self.status, ValidityStatus, "status")
        _require_reason_presence(
            self.status is ValidityStatus.CURRENT,
            self.stale_reason,
            "current validity must not have stale_reason",
            "stale validity requires stale_reason",
        )
        _require_text(self.stale_reason, "stale_reason", maximum=1024, optional=True)


@dataclass(frozen=True, slots=True)
class AnalyzerReceipt:
    receipt_sha256: str
    output_sha256: str
    source_manifest_sha256: str
    analysis_sha256: str

    def __post_init__(self) -> None:
        for name in ("receipt_sha256", "output_sha256", "source_manifest_sha256", "analysis_sha256"):
            _require_sha256(getattr(self, name), name)


Claim = SymbolClaim | RelationshipClaim | Diagnostic


@dataclass(frozen=True, slots=True)
class NormalizedAnalysis:
    run: AnalysisRun
    scopes: tuple[AnalysisScope, ...]
    coverage: tuple[Coverage, ...]
    symbols: tuple[SymbolClaim, ...]
    relationships: tuple[RelationshipClaim, ...]
    diagnostics: tuple[Diagnostic, ...]
    validity: tuple[Validity, ...]
    receipt: AnalyzerReceipt | None

    def __post_init__(self) -> None:
        _require_record(self.run, AnalysisRun, "run must be AnalysisRun")
        self._require_collections()
        scope_by_id = self._scope_index()
        claims = self.all_claims()
        claim_by_id = self._claim_index(claims, scope_by_id)
        self._require_related_sources(scope_by_id)
        self._require_validity(claim_by_id)
        self._require_coverage(claims)
        self._require_receipt()

    def _collections(self) -> tuple[tuple[tuple, str, type, Callable[[object], object]], ...]:
        return (
            (self.scopes, "scopes", AnalysisScope, lambda item: item.scope_id),
            (
                self.coverage,
                "coverage",
                Coverage,
                lambda item: (item.scope_id, item.source_id, item.capability.value),
            ),
            (self.symbols, "symbols", SymbolClaim, lambda item: item.claim_id),
            (
                self.relationships,
                "relationships",
                RelationshipClaim,
                lambda item: item.claim_id,
            ),
            (self.diagnostics, "diagnostics", Diagnostic, lambda item: item.diagnostic_id),
            (self.validity, "validity", Validity, lambda item: item.validity_id),
        )

    def _require_collections(self) -> None:
        for values, label, item_type, key in self._collections():
            _require_typed_tuple(values, label, item_type)
            _require_sorted_unique(values, label, key=key)
        if not self.scopes:
            raise ValueError("scopes must not be empty")

    def _scope_index(self) -> dict[str, AnalysisScope]:
        scope_by_id: dict[str, AnalysisScope] = {}
        build_scopes: set[tuple[str, str, str]] = set()
        for item in self.scopes:
            self._require_scope(item, build_scopes)
            build_scopes.add(_build_scope(item))
            scope_by_id[item.scope_id] = item
        return scope_by_id

    def _require_scope(self, item: object, build_scopes: set[tuple[str, str, str]]) -> None:
        run = self.run
        _require_all((
            (lambda: isinstance(item, AnalysisScope), TypeError("scopes must contain AnalysisScope")),
            (
                lambda: item.run_id == run.run_id,
                ValueError("scope run_id must match analysis run_id"),
            ),
            (
                lambda: item.source_manifest_sha256 == run.identity.source_manifest_sha256,
                ValueError("scope source_manifest_sha256 must match analysis identity"),
            ),
            (
                lambda: (item.analyzer_support != "unqualified") is run.qualified,
                ValueError("run qualified state must agree with scope analyzer_support"),
            ),
            (
                lambda: _build_scope(item) not in build_scopes,
                ValueError(
                    "analysis scopes must have unique run_id, build_target, and build_configuration"
                ),
            ),
        ))

    def _claim_index(
        self, claims: tuple[Claim, ...], scope_by_id: Mapping[str, AnalysisScope]
    ) -> dict[str, Claim]:
        claim_by_id: dict[str, Claim] = {}
        for claim in claims:
            self._require_claim(claim, scope_by_id)
            claim_id = _claim_id(claim)
            if claim_id in claim_by_id:
                raise ValueError("claim IDs must be unique across claim kinds")
            claim_by_id[claim_id] = claim
        return claim_by_id

    def _require_claim(self, claim: Claim, scope_by_id: Mapping[str, AnalysisScope]) -> None:
        run = self.run
        current_scope = scope_by_id.get(claim.scope_id)
        _require_all((
            (
                lambda: claim.run_id == run.run_id,
                ValueError("claim run_id must match analysis run_id"),
            ),
            (
                lambda: current_scope is not None,
                ValueError("claim scope_id must identify an analysis scope"),
            ),
            (
                lambda: claim.source_id in current_scope.expected_source_ids,
                ValueError("claim source_id must be expected by its scope"),
            ),
            (
                lambda: claim.capability in run.declared_capabilities,
                ValueError("claim capability must be declared by its run"),
            ),
        ))

    def _require_related_sources(self, scope_by_id: Mapping[str, AnalysisScope]) -> None:
        for item in self.diagnostics:
            expected_source_ids = scope_by_id[item.scope_id].expected_source_ids
            if any(related.source_id not in expected_source_ids for related in item.related):
                raise ValueError("diagnostic related source_id must be expected by its scope")

    def _require_validity(self, claim_by_id: Mapping[str, Claim]) -> None:
        validity_subjects: set[str] = set()
        for item in self.validity:
            _require_validity_subject(item, claim_by_id, validity_subjects)
            validity_subjects.add(item.subject_id)
        if validity_subjects != set(claim_by_id):
            raise ValueError("every claim must have exactly one validity record")

    def _expected_coverage(self) -> set[tuple[str, str, Capability]]:
        return {
            (item.scope_id, source_id, capability)
            for item in self.scopes
            for source_id in item.expected_source_ids
            for capability in self.run.declared_capabilities
        }

    def _coverage_index(self) -> dict[tuple[str, str, Capability], Coverage]:
        coverage_by_key: dict[tuple[str, str, Capability], Coverage] = {}
        for row in self.coverage:
            _require_record(row, Coverage, "coverage must contain Coverage")
            coverage_by_key[(row.scope_id, row.source_id, row.capability)] = row
        return coverage_by_key

    def _require_coverage(self, claims: tuple[Claim, ...]) -> None:
        expected_coverage = self._expected_coverage()
        coverage_by_key = self._coverage_index()
        if set(coverage_by_key) != expected_coverage:
            raise ValueError("coverage must exactly cover every scope source and declared capability")
        for claim in claims:
            key = (claim.scope_id, claim.source_id, claim.capability)
            if coverage_by_key[key].status is CoverageStatus.EXCLUDED:
                raise ValueError("excluded coverage cannot own claims")

    def _require_receipt(self) -> None:
        if self.receipt is not None:
            self._require_matching_receipt()
            return
        if self.run.analysis_mode == "precise":
            raise ValueError("precise normalized analysis requires an analyzer receipt")

    def _require_matching_receipt(self) -> None:
        receipt = self.receipt
        run = self.run
        _require_record(receipt, AnalyzerReceipt, "receipt must be AnalyzerReceipt")
        _require_all((
            (
                lambda: receipt.source_manifest_sha256 == run.identity.source_manifest_sha256,
                ValueError("receipt source_manifest_sha256 must match analysis identity"),
            ),
            (
                lambda: receipt.analysis_sha256 == run.identity.analysis_sha256,
                ValueError("receipt analysis_sha256 must match analysis identity"),
            ),
            (
                lambda: run.receipt_sha256 == receipt.receipt_sha256,
                ValueError("receipt hash must match analysis run"),
            ),
            (
                lambda: run.receipt_output_sha256 == receipt.output_sha256,
                ValueError("receipt output hash must match analysis run"),
            ),
        ))

    def all_claims(self) -> tuple[Claim, ...]:
        return self.symbols + self.relationships + self.diagnostics


_VERIFIED_BATCH_MINT = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedAnalysisBatch:
    analysis: NormalizedAnalysis
    analysis_mode: str
    source_manifest_sha256: str
    analysis_sha256: str
    receipt_sha256: str | None
    consent_grant_id: str | None
    consent_revision: int | None
    lease_id: str | None

    def __init__(
        self,
        analysis: NormalizedAnalysis,
        *,
        analysis_mode: str,
        source_manifest_sha256: str,
        analysis_sha256: str,
        receipt_sha256: str | None,
        consent_grant_id: str | None,
        consent_revision: int | None,
        lease_id: str | None,
        _mint: object,
    ) -> None:
        _require_all((
            (
                lambda: _mint is _VERIFIED_BATCH_MINT,
                TypeError("VerifiedAnalysisBatch is created by internal verifiers only"),
            ),
            (
                lambda: isinstance(analysis, NormalizedAnalysis),
                TypeError("analysis must be NormalizedAnalysis"),
            ),
            (
                lambda: analysis_mode == analysis.run.analysis_mode,
                ValueError("batch analysis_mode must match analysis run"),
            ),
            (
                lambda: source_manifest_sha256 == analysis.run.identity.source_manifest_sha256,
                ValueError("batch source_manifest_sha256 must match analysis identity"),
            ),
            (
                lambda: analysis_sha256 == analysis.run.identity.analysis_sha256,
                ValueError("batch analysis_sha256 must match analysis identity"),
            ),
        ))
        _require_batch_consent(
            analysis.run,
            analysis_mode,
            (receipt_sha256, consent_grant_id, consent_revision, lease_id),
        )
        object.__setattr__(self, "analysis", analysis)
        object.__setattr__(self, "analysis_mode", analysis_mode)
        object.__setattr__(self, "source_manifest_sha256", source_manifest_sha256)
        object.__setattr__(self, "analysis_sha256", analysis_sha256)
        object.__setattr__(self, "receipt_sha256", receipt_sha256)
        object.__setattr__(self, "consent_grant_id", consent_grant_id)
        object.__setattr__(self, "consent_revision", consent_revision)
        object.__setattr__(self, "lease_id", lease_id)


def verify_native_analysis(
    snapshot: CorpusSnapshot,
    analysis: NormalizedAnalysis,
) -> VerifiedAnalysisBatch:
    _require_record(snapshot, CorpusSnapshot, "snapshot must be CorpusSnapshot")
    _require_record(analysis, NormalizedAnalysis, "analysis must be NormalizedAnalysis")
    for source in snapshot.sources:
        _require_captured_content(source)
    _require_canonical_manifest(snapshot)
    _require_native_run(analysis.run, snapshot)
    captured = _CapturedSources(snapshot)
    captured.require_scopes(analysis.scopes)
    captured.require_claim_ranges(analysis)
    if any(item.evidence_level is not EvidenceLevel.SYNTAX for item in analysis.all_claims()):
        raise ValueError("native verification accepts syntax evidence only")
    return VerifiedAnalysisBatch(
        analysis,
        analysis_mode="native-syntax",
        source_manifest_sha256=analysis.run.identity.source_manifest_sha256,
        analysis_sha256=analysis.run.identity.analysis_sha256,
        receipt_sha256=None,
        consent_grant_id=None,
        consent_revision=None,
        lease_id=None,
        _mint=_VERIFIED_BATCH_MINT,
    )


def _require_captured_content(source: object) -> None:
    if type(source.record.size) is not int or source.record.size != len(source.content):
        raise ValueError(f"captured source content size mismatch: {source.record.logical_id}")
    if source.record.sha256 != hashlib.sha256(source.content).hexdigest():
        raise ValueError(f"captured source content sha256 mismatch: {source.record.logical_id}")


def _require_canonical_manifest(snapshot: CorpusSnapshot) -> None:
    canonical_manifest = canonical_source_manifest_sha256(
        (source.record for source in snapshot.sources),
        snapshot.policy,
        collector_version=snapshot.collector_version,
        extractor_version=snapshot.extractor_version,
    )
    if canonical_manifest != snapshot.corpus_sha256:
        raise ValueError("snapshot corpus_sha256 does not match canonical source manifest")


def _require_native_run(run: AnalysisRun, snapshot: CorpusSnapshot) -> None:
    _require_all((
        (
            lambda: run.analysis_mode == "native-syntax",
            ValueError("native verification requires a native-syntax run"),
        ),
        (
            lambda: run.identity.source_manifest_sha256 == snapshot.corpus_sha256,
            ValueError("native analysis does not match captured source manifest"),
        ),
        (
            lambda: run.evidence_level is EvidenceLevel.SYNTAX,
            ValueError("native verification requires run syntax evidence"),
        ),
    ))


class _CapturedSources:
    """The captured bytes every native claim must point into."""

    def __init__(self, snapshot: CorpusSnapshot) -> None:
        self.by_id = {source.record.logical_id: source for source in snapshot.sources}

    def length(self, source_id: str, label: str) -> int:
        source = self.by_id.get(source_id)
        if source is None:
            raise ValueError(f"{source_id} is not captured for {label}")
        return len(source.captured_bytes)

    def require_range(self, source_id: str, position: PositionRange, label: str) -> None:
        if position.byte_end > self.length(source_id, label):
            raise ValueError(f"{label} range exceeds captured bytes for {source_id}")

    def require_scopes(self, scopes: tuple[AnalysisScope, ...]) -> None:
        for scope in scopes:
            self._require_expected_sources(scope)

    def _require_expected_sources(self, scope: AnalysisScope) -> None:
        for expected in scope.expected_sources:
            self.length(expected.source_id, "analysis scope")
            if self.by_id[expected.source_id].record.sha256 != expected.source_sha256:
                raise ValueError(
                    f"expected source sha256 does not match capture: {expected.source_id}"
                )

    def require_claim_ranges(self, analysis: NormalizedAnalysis) -> None:
        for row in analysis.coverage:
            self.length(row.source_id, "coverage")
        for label, claims in (("symbol", analysis.symbols), ("relationship", analysis.relationships)):
            self._require_ranges(claims, label)
        for item in analysis.diagnostics:
            self.require_range(item.source_id, item.range, "diagnostic")
            self._require_ranges(item.related, "related")

    def _require_ranges(self, items: Iterable[object], label: str) -> None:
        for item in items:
            self.require_range(item.source_id, item.range, label)


def closed_world(
    scope: AnalysisScope,
    coverage: Sequence[Coverage],
    capability: Capability,
) -> bool:
    _require_record(scope, AnalysisScope, "scope must be AnalysisScope")
    _require_enum(capability, Capability, "capability")
    rows = _capability_rows(scope, coverage, capability)
    source_ids = tuple(row.source_id for row in rows)
    if len(source_ids) != len(set(source_ids)):
        return False
    return (
        _closed_scope(scope)
        and set(source_ids) == set(scope.expected_source_ids)
        and _closed_rows(rows)
    )


def _capability_rows(
    scope: AnalysisScope, coverage: Sequence[Coverage], capability: Capability
) -> tuple[Coverage, ...]:
    return tuple(
        row
        for row in coverage
        if row.scope_id == scope.scope_id and row.capability is capability
    )


def _closed_scope(scope: AnalysisScope) -> bool:
    return (
        scope.generated_sources in {"available", "not-required"}
        and scope.dependency_resolution == "complete"
        and scope.analyzer_support == "complete"
    )


def _closed_rows(rows: tuple[Coverage, ...]) -> bool:
    terminal = all(
        row.status in {CoverageStatus.COMPLETE, CoverageStatus.EXCLUDED} for row in rows
    )
    return terminal and all(row.closed_world_eligible for row in rows)


def _require_identity(identity: object) -> None:
    _require_record(identity, AnalysisIdentity, "identity must be AnalysisIdentity")
    if identity.recompute_analysis_sha256() != identity.analysis_sha256:
        raise ValueError("identity analysis_sha256 does not match its components")


def _require_protocol(protocol: object) -> None:
    _require_text(protocol, "protocol", maximum=32)
    if protocol not in _PROTOCOLS:
        raise ValueError(f"invalid protocol: {protocol!r}")


def _capability_key(item: object) -> str:
    return item.value if isinstance(item, Capability) else repr(item)


def _require_declared_capabilities(capabilities: object) -> None:
    _require_sorted_unique(capabilities, "declared_capabilities", key=_capability_key)
    if not capabilities:
        raise ValueError("declared_capabilities must not be empty")
    for capability in capabilities:
        _require_enum(capability, Capability, "declared capability")


def _require_run_times(started_at: object, ended_at: object) -> None:
    _require_text(started_at, "started_at", maximum=64)
    _require_text(ended_at, "ended_at", maximum=64)
    if ended_at < started_at:
        raise ValueError("ended_at must not precede started_at")


def _require_relationship_target(
    resolution: RelationshipResolution, target_identity: object, target_text: object
) -> None:
    if resolution is RelationshipResolution.RESOLVED:
        _require_resolved_target(target_identity, target_text)
        return
    if target_identity is not None or target_text is None:
        raise ValueError("unresolved or ambiguous relationship requires only target text")
    _require_text(target_text, "target text", maximum=4096)


def _require_resolved_target(target_identity: object, target_text: object) -> None:
    if not isinstance(target_identity, SymbolIdentity) or target_text is not None:
        raise ValueError("resolved relationship requires only a target identity")


def _related_location_key(item: object) -> tuple:
    if not isinstance(item, RelatedLocation):
        return (repr(item),)
    return (item.source_id, item.range.byte_start, item.range.byte_end, item.message or "")


def _build_scope(item: AnalysisScope) -> tuple[str, str, str]:
    return (item.run_id, item.build_target, item.build_configuration)


def _claim_id(claim: Claim) -> str:
    if isinstance(claim, Diagnostic):
        return claim.diagnostic_id
    return claim.claim_id


_SUBJECT_TYPES = {
    SubjectKind.SYMBOL: SymbolClaim,
    SubjectKind.RELATIONSHIP: RelationshipClaim,
    SubjectKind.DIAGNOSTIC: Diagnostic,
}


def _require_validity_subject(
    item: object, claim_by_id: Mapping[str, Claim], validity_subjects: set[str]
) -> None:
    _require_record(item, Validity, "validity must contain Validity")
    subject = claim_by_id.get(item.subject_id)
    _require_all((
        (
            lambda: subject is not None and isinstance(subject, _SUBJECT_TYPES[item.subject_kind]),
            ValueError("validity subject kind and ID must identify one claim"),
        ),
        (
            lambda: item.subject_id not in validity_subjects,
            ValueError("validity subjects must be unique"),
        ),
    ))


def _require_batch_consent(
    run: AnalysisRun, analysis_mode: str, consent: tuple[object, ...]
) -> None:
    if analysis_mode == "precise":
        _require_precise_consent(run, consent)
        return
    _require_native_consent(analysis_mode, consent)


def _require_native_consent(analysis_mode: str, consent: tuple[object, ...]) -> None:
    if analysis_mode != "native-syntax":
        raise ValueError(f"invalid analysis_mode: {analysis_mode!r}")
    if any(item is not None for item in consent):
        raise ValueError("native-syntax batch must not contain receipt, consent, or lease")


def _require_precise_consent(run: AnalysisRun, consent: tuple[object, ...]) -> None:
    expected = (run.receipt_sha256, run.consent_grant_id, run.consent_revision, run.lease_id)
    if any(given != recorded for given, recorded in zip(consent, expected)):
        raise ValueError("precise batch must match run receipt and consent identity")
