"""Provider result and planning value records, independent of transport."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from context_budget import TokenCount, TokenUsage
from llm_wiki.formats.canonical import canonical_json_bytes


@dataclass(frozen=True)
class ProviderDescriptor:
    """Resolved identity and behavior of one provider attempt."""

    provider: str
    model: str | None
    capabilities: Mapping[str, object]
    inference_settings: Mapping[str, object]
    candidate_index: int
    fallback_from: tuple[str, ...]
    _endpoint: str | None = field(default=None, repr=False, compare=False)
    _resolution_failure: str | None = field(default=None, repr=False, compare=False)
    _codex_basis: CodexPlanningBasis | None = field(default=None, repr=False, compare=False)

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model or '<implicit>'}"

    @property
    def resolution_failure(self) -> str | None:
        return self._resolution_failure

    def canonical(self) -> dict[str, object]:
        """Return the descriptor in the restricted JSON value domain."""
        value = {
            "provider": self.provider,
            "model": self.model,
            "capabilities": dict(self.capabilities),
            "inference_settings": dict(self.inference_settings),
            "candidate_index": self.candidate_index,
            "fallback_from": list(self.fallback_from),
        }
        return json.loads(canonical_json_bytes(value))


@dataclass(frozen=True)
class LLMResult:
    """Outcome of exactly one provider candidate call."""

    descriptor: ProviderDescriptor
    text: str | None
    available: bool
    failure_class: str | None
    structured_output: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    input_token_count: TokenCount | None = None
    # The redacted cause behind `failure_class` when an exception produced it:
    # the class names what failed, this says why.
    failure_detail: str | None = None


@dataclass(frozen=True)
class BackendResponse:
    """Internal response carrying provider text and reported usage."""

    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)


@dataclass(frozen=True)
class CodexExecutable:
    path: str
    resolved_path: str
    identity: tuple[int, ...]
    sha256: str


@dataclass(frozen=True)
class CodexPlanningBasis:
    """Attempt-owned native model/catalog evidence; advertised planning, not wire capacity."""

    original_descriptor: ProviderDescriptor
    executable: CodexExecutable
    model: str
    model_provider: str
    planning_window: int | None
    catalog_sha256: str
    config_files: tuple[tuple[str, str | None], ...]
    environment_sha256: str
    cli_version: str
    mcp_server_names: tuple[str, ...] = ()

    @property
    def descriptor(self) -> ProviderDescriptor:
        return replace(self.original_descriptor, model=self.model, _codex_basis=self)


# Retained public import and serialized class identity; one implementation.
ProviderDescriptor.__module__ = "llm_client"
LLMResult.__module__ = "llm_client"
BackendResponse.__module__ = "llm_client"
CodexExecutable.__module__ = "llm_client"
CodexPlanningBasis.__module__ = "llm_client"
