"""Value records and shared bounds of a language-server session: documents, locations, answers."""

import math
from dataclasses import dataclass

from lsp_positions import LspRange
from lsp_security import RepositorySource

# Cleanup budget of an owner that failed to start. basis unknown — value predates measurement; review when failed startups leave owners registered.
OWNER_CLEANUP_SECONDS = 2.0
# How often a caller waiting on a session lock looks again; always capped by its own deadline.
LOCK_POLL_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class OpenDocument:
    source: RepositorySource
    content: bytes
    source_sha256: str
    version: int


@dataclass(frozen=True, slots=True)
class LspLocation:
    uri: str
    range: LspRange


@dataclass(frozen=True, slots=True)
class ProviderLocations:
    locations: tuple[LspLocation, ...]
    coverage: str
    partial: bool


@dataclass(frozen=True, slots=True)
class ProviderHover:
    contents: str | None
    range: LspRange | None
    partial: bool


@dataclass(frozen=True, slots=True)
class ProviderCalls:
    direction: str
    locations: tuple[LspLocation, ...]
    coverage: str
    partial: bool


@dataclass(frozen=True, slots=True)
class LspDiagnostic:
    uri: str
    range: LspRange
    severity: int | None
    code: str | None
    message: str
    related: tuple[tuple[LspLocation, str | None], ...]


@dataclass(frozen=True, slots=True)
class ProviderDiagnostics:
    diagnostics: tuple[LspDiagnostic, ...]
    document_version: int | None
    partial: bool


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    diagnostics: tuple[LspDiagnostic, ...]
    document_version: int | None
    partial: bool
    retained_bytes: int


def validated_deadline(deadline: float) -> float:
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        raise TypeError("deadline must be a monotonic timestamp")
    if not math.isfinite(deadline):
        raise ValueError("deadline must be finite")
    return float(deadline)


class BootstrapDegradation(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
