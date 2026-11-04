"""Pure compile event values, canonical bytes and validation; no journal IO."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

_STAGES = frozenset((
    'pending', 'snapshot_ready', 'draft_ready', 'review_ready',
    'publication_ready', 'published',
))

_OUTCOMES = frozenset(('ready', 'running', 'waiting', 'blocked', 'failed', 'completed'))

_DIGEST = re.compile(r'[0-9a-f]{64}\Z')

class ProgressConflict(ValueError):
    """The caller no longer holds the progress revision it expected."""

class ProgressCorrupt(ValueError):
    """Stored checkpoint bytes or their identity failed validation."""

class SourceOutcomeConflict(ProgressConflict):
    """Another work may already have paid for an overlapping source span."""

    def __init__(self, event):
        self.work_id = event.work_id
        self.sequence = event.sequence
        super().__init__(f'source outcome unresolved in work {event.work_id} at {event.sequence}')

@dataclass(frozen=True)
class CompileEvent:
    work_id: str
    task_id: str
    sequence: int
    source_digest: str
    stage: str
    outcome: str
    details: dict[str, Any]

def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False)

def _event_digest(event: CompileEvent) -> str:
    return hashlib.sha256(_canonical(asdict(event)).encode('utf-8')).hexdigest()

def _require_digest(value: object) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError('compile identity must be a SHA-256 hex digest')

def _require_sequence(value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError('compile sequence must be a nonnegative integer')

def _require_labels(event: CompileEvent) -> None:
    if event.stage not in _STAGES:
        raise ValueError('unknown compile stage')
    if event.outcome not in _OUTCOMES:
        raise ValueError('unknown compile outcome')

def _require_details(event: CompileEvent) -> None:
    if not isinstance(event.details, dict):
        raise TypeError('compile event details must be an object')
    if not isinstance(event.task_id, str) or not event.task_id:
        raise ValueError('compile task identity must be nonempty text')
    _canonical(event.details)

def _validate_event(event: CompileEvent) -> None:
    _require_digest(event.work_id)
    _require_digest(event.source_digest)
    _require_sequence(event.sequence)
    _require_labels(event)
    _require_details(event)

def _snapshot_event(event: CompileEvent) -> CompileEvent:
    _validate_event(event)
    snapshot = CompileEvent(**json.loads(_canonical(asdict(event))))
    if snapshot != event:
        raise ValueError('compile event must contain lossless JSON values')
    return snapshot

def artifact_identity(event: CompileEvent) -> tuple[str, str]:
    """Accept only a queue-result filename, never an arbitrary path from metadata."""
    try:
        artifact = event.details['artifact']
        relative, digest = artifact['reference'], artifact['sha256']
        _require_digest(digest)
        safe = re.fullmatch(r'run/queue-results/[0-9a-f]{64}\.result', relative)
    except (KeyError, TypeError, ValueError) as error:
        raise ProgressCorrupt('invalid compile artifact reference') from error
    if safe is None:
        raise ProgressCorrupt('compile artifact path escapes result storage')
    return relative, digest

def _require_model_phase(event):
    call = event.details['model_call']
    if not isinstance(call, dict):
        raise ProgressCorrupt('invalid retained model call')
    if call.get('phase') not in {'dispatch', 'answer'}:
        raise ProgressCorrupt('invalid retained model call phase')


# Historical facade identity is retained for existing pickle consumers.
ProgressConflict.__module__ = 'compile_progress'
ProgressCorrupt.__module__ = 'compile_progress'
SourceOutcomeConflict.__module__ = 'compile_progress'
CompileEvent.__module__ = 'compile_progress'
