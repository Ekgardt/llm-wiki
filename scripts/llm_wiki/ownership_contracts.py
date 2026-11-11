"""Ownership values and error contracts; authority remains with the registry."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from llm_wiki.runtime_contracts import RuntimeFileIdentity

OwnerRole = Literal[
    "capture",
    "project",
    "markdown-writer",
    "queue-worker",
    "compile",
    "doctor",
    "nightly",
    "weekly",
    "lsp",
    "queue-operator",
    "repair",
    "runtime-deletion-check",
]


ProcessState = Literal["alive", "dead", "unknown"]


class OperationalOwnershipError(RuntimeError):
    """Stable failure from canonical operational ownership."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)

    def __reduce__(self):
        return (self.__class__, (self.code, str(self)))


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_identity: str


@dataclass(frozen=True)
class OwnerLease:
    state_root: Path
    role: OwnerRole
    scope: str
    actor_id: str
    token: str
    epoch: int
    process: ProcessIdentity
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    ttl_seconds: int
    heartbeat_seconds: int


@dataclass(frozen=True)
class MarkerIdentity:
    relative_path: str
    sha256: str
    file_identity: RuntimeFileIdentity
    pid: int


# Historical public import paths remain the serialization identity.
__all__ = ['OwnerRole', 'ProcessState', 'OperationalOwnershipError', 'ProcessIdentity', 'OwnerLease', 'MarkerIdentity']
OperationalOwnershipError.__module__ = 'operational_ownership'
ProcessIdentity.__module__ = 'operational_ownership'
OwnerLease.__module__ = 'operational_ownership'
MarkerIdentity.__module__ = 'operational_ownership'
