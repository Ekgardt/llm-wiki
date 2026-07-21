"""Immutable normalized contracts for code-intelligence analysis."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, unique

from corpus_snapshot import CorpusSnapshot
from reliable_memory import canonical_json_bytes

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_GENERATED_SOURCES = frozenset({"available", "unavailable", "not-required"})
_DEPENDENCY_RESOLUTION = frozenset({"complete", "partial", "unavailable"})
_ANALYZER_SUPPORT = frozenset({"complete", "partial", "unsupported", "unqualified"})
_ANALYSIS_MODES = frozenset({"precise", "native-syntax"})
_PROTOCOLS = frozenset({"scip", "lsp", "native"})


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
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if not encoded:
        raise ValueError(f"{label} must not be empty")
    if len(encoded) > maximum:
        raise ValueError(f"{label} exceeds {maximum} UTF-8 bytes")
    if not unicodedata.is_normalized("NFC", value):
        raise ValueError(f"{label} must use NFC normalization")
    return value


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


def _require_choice(value: object, choices: frozenset[str], label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if value not in choices:
        raise ValueError(f"invalid {label}: {value!r}")


def _require_sorted_unique(values: object, label: str, *, key) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{label} must be a tuple")
    keys = tuple(key(item) for item in values)
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
class AnalysisScope:
    scope_id: str
    run_id: str
    source_manifest_sha256: str
    build_target: str
    build_configuration: str
    expected_source_ids: tuple[str, ...]
    generated_sources: str
    dependency_resolution: str
    analyzer_support: str

    def __post_init__(self) -> None:
        _require_id(self.scope_id, "scope_id")
        _require_id(self.run_id, "run_id")
        _require_sha256(self.source_manifest_sha256, "source_manifest_sha256")
        _require_text(self.build_target, "build_target", maximum=256)
        _require_text(self.build_configuration, "build_configuration", maximum=256)
        _require_sorted_unique(self.expected_source_ids, "expected_source_ids", key=lambda item: item)
        for source_id in self.expected_source_ids:
            _require_id(source_id, "expected source_id")
        _require_choice(self.generated_sources, _GENERATED_SOURCES, "generated_sources")
        _require_choice(self.dependency_resolution, _DEPENDENCY_RESOLUTION, "dependency_resolution")
        _require_choice(self.analyzer_support, _ANALYZER_SUPPORT, "analyzer_support")


@dataclass(frozen=True, slots=True)
class PositionRange:
    byte_start: int
    byte_end: int

    def __post_init__(self) -> None:
        for name in ("byte_start", "byte_end"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
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
        if terminal and self.reason is not None:
            raise ValueError("complete or excluded coverage must not have a reason")
        if not terminal and self.reason is None:
            raise ValueError("incomplete coverage must have a reason")
        if self.reason is not None:
            _require_text(self.reason, "coverage reason", maximum=1024)
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
        if not isinstance(self.identity, AnalysisIdentity):
            raise TypeError("identity must be AnalysisIdentity")
        if self.identity.recompute_analysis_sha256() != self.identity.analysis_sha256:
            raise ValueError("identity analysis_sha256 does not match its components")
        _require_sha256(self.source_manifest_sha256, "source_manifest_sha256")
        if self.source_manifest_sha256 != self.identity.source_manifest_sha256:
            raise ValueError("run source_manifest_sha256 must match identity")
        _require_choice(self.analysis_mode, _ANALYSIS_MODES, "analysis_mode")
        for name in ("repository_id", "checkout_id", "source_generation_id"):
            _require_id(getattr(self, name), name)
        for name in ("analyzer_family", "analyzer_version", "protocol_version"):
            _require_text(getattr(self, name), name, maximum=256)
        _require_text(self.protocol, "protocol", maximum=32)
        if self.protocol not in _PROTOCOLS:
            raise ValueError(f"invalid protocol: {self.protocol!r}")
        _require_sha256(self.executable_sha256, "executable_sha256")
        _require_sorted_unique(
            self.declared_capabilities,
            "declared_capabilities",
            key=lambda item: item.value if isinstance(item, Capability) else repr(item),
        )
        if not self.declared_capabilities:
            raise ValueError("declared_capabilities must not be empty")
        for capability in self.declared_capabilities:
            _require_enum(capability, Capability, "declared capability")
        _require_enum(self.evidence_level, EvidenceLevel, "evidence_level")
        _require_bool(self.qualified, "qualified")
        _require_enum(self.outcome, AnalysisOutcome, "outcome")
        for name in ("started_at", "ended_at"):
            _require_text(getattr(self, name), name, maximum=64)
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        self._validate_mode()

    def _validate_mode(self) -> None:
        consent = (self.receipt_sha256, self.receipt_output_sha256, self.consent_grant_id, self.lease_id)
        if self.analysis_mode == "precise":
            if self.evidence_level not in {EvidenceLevel.COMPILER, EvidenceLevel.SEMANTIC}:
                raise ValueError("precise analysis requires compiler or semantic evidence")
            if any(item is None for item in consent) or self.consent_revision is None:
                raise ValueError("precise analysis requires receipt, consent grant, revision, and lease")
            _require_sha256(self.receipt_sha256, "receipt_sha256")
            _require_sha256(self.receipt_output_sha256, "receipt_output_sha256")
            _require_id(self.consent_grant_id, "consent_grant_id")
            _require_id(self.lease_id, "lease_id")
            if isinstance(self.consent_revision, bool) or not isinstance(self.consent_revision, int):
                raise TypeError("consent_revision must be an integer")
            if self.consent_revision < 1:
                raise ValueError("consent_revision must be positive")
            return
        if self.evidence_level not in {EvidenceLevel.SYNTAX, EvidenceLevel.LEXICAL}:
            raise ValueError("native-syntax analysis requires syntax or lexical evidence")
        if any(item is not None for item in consent) or self.consent_revision is not None:
            raise ValueError("native-syntax analysis must not contain receipt, consent, or lease")


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
        if not isinstance(self.identity, SymbolIdentity):
            raise TypeError("identity must be SymbolIdentity")
        _require_text(self.display_name, "display_name", maximum=1024)
        _require_text(self.symbol_kind, "symbol_kind", maximum=128)
        _require_enum(self.role, SymbolRole, "role")
        required = {
            SymbolRole.DEFINITION: Capability.DEFINITIONS,
            SymbolRole.DECLARATION: Capability.DECLARATIONS,
        }[self.role]
        if self.capability is not required:
            raise ValueError("symbol capability must agree with role")
        if not isinstance(self.range, PositionRange):
            raise TypeError("range must be PositionRange")
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
        if not isinstance(self.source_identity, SymbolIdentity):
            raise TypeError("source_identity must be SymbolIdentity")
        _require_enum(self.relation, Relationship, "relation")
        _require_enum(self.capability, Capability, "capability")
        if self.capability is not _RELATION_CAPABILITY[self.relation]:
            raise ValueError("relationship capability must agree with relation")
        _require_enum(self.resolution, RelationshipResolution, "resolution")
        if self.resolution is RelationshipResolution.RESOLVED:
            if not isinstance(self.target_identity, SymbolIdentity) or self.target_text is not None:
                raise ValueError("resolved relationship requires only a target identity")
        else:
            if self.target_identity is not None or self.target_text is None:
                raise ValueError("unresolved or ambiguous relationship requires only target text")
            _require_text(self.target_text, "target text", maximum=4096)
        if not isinstance(self.range, PositionRange):
            raise TypeError("range must be PositionRange")
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
        if self.code is not None:
            _require_text(self.code, "diagnostic code", maximum=256)
        _require_text(self.message, "diagnostic message", maximum=8192)
        if not isinstance(self.range, PositionRange):
            raise TypeError("range must be PositionRange")
        self.range.require_nonempty("diagnostic range")
        _require_enum(self.evidence_level, EvidenceLevel, "evidence_level")
        if self.evidence_level is EvidenceLevel.LEXICAL:
            raise ValueError("diagnostic evidence cannot be lexical")
        _require_sorted_unique(
            self.related,
            "diagnostic related locations",
            key=lambda item: (
                item.source_id,
                item.range.byte_start,
                item.range.byte_end,
                item.message or "",
            )
            if isinstance(item, RelatedLocation)
            else (repr(item),),
        )
        for item in self.related:
            if not isinstance(item, RelatedLocation):
                raise TypeError("diagnostic related locations must contain RelatedLocation")


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
        if self.status is ValidityStatus.CURRENT and self.stale_reason is not None:
            raise ValueError("current validity must not have stale_reason")
        if self.status is not ValidityStatus.CURRENT and self.stale_reason is None:
            raise ValueError("stale validity requires stale_reason")
        if self.stale_reason is not None:
            _require_text(self.stale_reason, "stale_reason", maximum=1024)


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
        if not isinstance(self.run, AnalysisRun):
            raise TypeError("run must be AnalysisRun")
        collections = (
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
        for values, label, item_type, key in collections:
            if not isinstance(values, tuple):
                raise TypeError(f"{label} must be a tuple")
            if any(not isinstance(item, item_type) for item in values):
                raise TypeError(f"{label} contains an invalid record type")
            _require_sorted_unique(values, label, key=key)
        if not self.scopes:
            raise ValueError("scopes must not be empty")
        scope_by_id: dict[str, AnalysisScope] = {}
        build_scopes: set[tuple[str, str, str]] = set()
        for item in self.scopes:
            if not isinstance(item, AnalysisScope):
                raise TypeError("scopes must contain AnalysisScope")
            if item.run_id != self.run.run_id:
                raise ValueError("scope run_id must match analysis run_id")
            if item.source_manifest_sha256 != self.run.identity.source_manifest_sha256:
                raise ValueError("scope source_manifest_sha256 must match analysis identity")
            build_scope = (item.run_id, item.build_target, item.build_configuration)
            if build_scope in build_scopes:
                raise ValueError(
                    "analysis scopes must have unique run_id, build_target, and build_configuration"
                )
            build_scopes.add(build_scope)
            scope_by_id[item.scope_id] = item

        claims = self.all_claims()
        claim_by_id: dict[str, Claim] = {}
        for claim in claims:
            if claim.run_id != self.run.run_id:
                raise ValueError("claim run_id must match analysis run_id")
            current_scope = scope_by_id.get(claim.scope_id)
            if current_scope is None:
                raise ValueError("claim scope_id must identify an analysis scope")
            if claim.source_id not in current_scope.expected_source_ids:
                raise ValueError("claim source_id must be expected by its scope")
            if claim.capability not in self.run.declared_capabilities:
                raise ValueError("claim capability must be declared by its run")
            claim_id = claim.claim_id if not isinstance(claim, Diagnostic) else claim.diagnostic_id
            if claim_id in claim_by_id:
                raise ValueError("claim IDs must be unique across claim kinds")
            claim_by_id[claim_id] = claim
        for item in self.diagnostics:
            expected_source_ids = scope_by_id[item.scope_id].expected_source_ids
            if any(related.source_id not in expected_source_ids for related in item.related):
                raise ValueError("diagnostic related source_id must be expected by its scope")

        validity_subjects: set[str] = set()
        subject_types = {
            SubjectKind.SYMBOL: SymbolClaim,
            SubjectKind.RELATIONSHIP: RelationshipClaim,
            SubjectKind.DIAGNOSTIC: Diagnostic,
        }
        for item in self.validity:
            if not isinstance(item, Validity):
                raise TypeError("validity must contain Validity")
            subject = claim_by_id.get(item.subject_id)
            if subject is None or not isinstance(subject, subject_types[item.subject_kind]):
                raise ValueError("validity subject kind and ID must identify one claim")
            if item.subject_id in validity_subjects:
                raise ValueError("validity subjects must be unique")
            validity_subjects.add(item.subject_id)
        if validity_subjects != set(claim_by_id):
            raise ValueError("every claim must have exactly one validity record")

        expected_coverage = {
            (item.scope_id, source_id, capability)
            for item in self.scopes
            for source_id in item.expected_source_ids
            for capability in self.run.declared_capabilities
        }
        actual_coverage: set[tuple[str, str, Capability]] = set()
        coverage_by_key: dict[tuple[str, str, Capability], Coverage] = {}
        for row in self.coverage:
            if not isinstance(row, Coverage):
                raise TypeError("coverage must contain Coverage")
            key = (row.scope_id, row.source_id, row.capability)
            actual_coverage.add(key)
            coverage_by_key[key] = row
        if actual_coverage != expected_coverage:
            raise ValueError("coverage must exactly cover every scope source and declared capability")
        for claim in claims:
            key = (claim.scope_id, claim.source_id, claim.capability)
            if coverage_by_key[key].status is CoverageStatus.EXCLUDED:
                raise ValueError("excluded coverage cannot own claims")

        if self.receipt is not None:
            if not isinstance(self.receipt, AnalyzerReceipt):
                raise TypeError("receipt must be AnalyzerReceipt")
            if self.receipt.source_manifest_sha256 != self.run.identity.source_manifest_sha256:
                raise ValueError("receipt source_manifest_sha256 must match analysis identity")
            if self.receipt.analysis_sha256 != self.run.identity.analysis_sha256:
                raise ValueError("receipt analysis_sha256 must match analysis identity")
            if self.run.receipt_sha256 != self.receipt.receipt_sha256:
                raise ValueError("receipt hash must match analysis run")
            if self.run.receipt_output_sha256 != self.receipt.output_sha256:
                raise ValueError("receipt output hash must match analysis run")
        elif self.run.analysis_mode == "precise":
            raise ValueError("precise normalized analysis requires an analyzer receipt")

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
        if _mint is not _VERIFIED_BATCH_MINT:
            raise TypeError("VerifiedAnalysisBatch is created by internal verifiers only")
        if not isinstance(analysis, NormalizedAnalysis):
            raise TypeError("analysis must be NormalizedAnalysis")
        if analysis_mode != analysis.run.analysis_mode:
            raise ValueError("batch analysis_mode must match analysis run")
        if source_manifest_sha256 != analysis.run.identity.source_manifest_sha256:
            raise ValueError("batch source_manifest_sha256 must match analysis identity")
        if analysis_sha256 != analysis.run.identity.analysis_sha256:
            raise ValueError("batch analysis_sha256 must match analysis identity")
        if analysis_mode == "precise":
            if (
                receipt_sha256 != analysis.run.receipt_sha256
                or consent_grant_id != analysis.run.consent_grant_id
                or consent_revision != analysis.run.consent_revision
                or lease_id != analysis.run.lease_id
            ):
                raise ValueError("precise batch must match run receipt and consent identity")
        elif analysis_mode == "native-syntax":
            if any(item is not None for item in (receipt_sha256, consent_grant_id, consent_revision, lease_id)):
                raise ValueError("native-syntax batch must not contain receipt, consent, or lease")
        else:
            raise ValueError(f"invalid analysis_mode: {analysis_mode!r}")
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
    if not isinstance(snapshot, CorpusSnapshot):
        raise TypeError("snapshot must be CorpusSnapshot")
    if not isinstance(analysis, NormalizedAnalysis):
        raise TypeError("analysis must be NormalizedAnalysis")
    if analysis.run.analysis_mode != "native-syntax":
        raise ValueError("native verification requires a native-syntax run")
    if analysis.run.identity.source_manifest_sha256 != snapshot.corpus_sha256:
        raise ValueError("native analysis does not match captured source manifest")
    if analysis.run.evidence_level is not EvidenceLevel.SYNTAX:
        raise ValueError("native verification requires run syntax evidence")
    source_by_id = {source.record.logical_id: source for source in snapshot.sources}

    def require_captured(source_id: str, label: str) -> int:
        source = source_by_id.get(source_id)
        if source is None:
            raise ValueError(f"{source_id} is not captured for {label}")
        return len(source.captured_bytes)

    def require_captured_range(
        source_id: str,
        position: PositionRange,
        label: str,
    ) -> None:
        if position.byte_end > require_captured(source_id, label):
            raise ValueError(f"{label} range exceeds captured bytes for {source_id}")

    for scope in analysis.scopes:
        for source_id in scope.expected_source_ids:
            require_captured(source_id, "analysis scope")
    for row in analysis.coverage:
        require_captured(row.source_id, "coverage")
    for item in analysis.symbols:
        require_captured_range(item.source_id, item.range, "symbol")
    for item in analysis.relationships:
        require_captured_range(item.source_id, item.range, "relationship")
    for item in analysis.diagnostics:
        require_captured_range(item.source_id, item.range, "diagnostic")
        for related in item.related:
            require_captured_range(related.source_id, related.range, "related")
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


def closed_world(
    scope: AnalysisScope,
    coverage: Sequence[Coverage],
    capability: Capability,
) -> bool:
    if not isinstance(scope, AnalysisScope):
        raise TypeError("scope must be AnalysisScope")
    _require_enum(capability, Capability, "capability")
    rows = tuple(
        row
        for row in coverage
        if row.scope_id == scope.scope_id and row.capability is capability
    )
    source_ids = tuple(row.source_id for row in rows)
    if len(source_ids) != len(set(source_ids)):
        return False
    return (
        scope.generated_sources in {"available", "not-required"}
        and scope.dependency_resolution == "complete"
        and scope.analyzer_support == "complete"
        and set(source_ids) == set(scope.expected_source_ids)
        and all(
            row.status in {CoverageStatus.COMPLETE, CoverageStatus.EXCLUDED}
            for row in rows
        )
        and all(row.closed_world_eligible for row in rows)
    )
