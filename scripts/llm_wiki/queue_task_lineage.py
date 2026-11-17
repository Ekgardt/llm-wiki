"""What one task inherits across redrives: compile lineage, capture decisions and seals."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence

import compile_progress
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import validate_payload_blob
from llm_wiki.queue_contracts import CaptureTaskBinding, CompileCompletionError, QueueOperationError
from llm_wiki.queue_validation import capture_decision_relative_path


def capture_semantic_seal_digest(
    task_id: str, intent_id: str, stage: str, active_link_digest: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "active_digest": active_link_digest,
                "consumer_id": f"{intent_id}:{stage}",
                "consumer_kind": "semantic-decision",
                "task_id": task_id,
            }
        )
    )


def require_indexed_capture_decision(
    row: sqlite3.Row,
    active: CaptureTaskBinding,
    *,
    intent_id: str,
    stage: str,
    active_link_digest: str,
) -> None:
    decision_path = capture_decision_relative_path(intent_id, stage)
    seal_digest = capture_semantic_seal_digest(
        active.task_id, intent_id, stage, active_link_digest
    )
    actual = (
        active.intent_id,
        active.active_digest,
        active.seal_digest,
        row["decision_path"],
        row["active_link_digest"],
        row["publication_state"],
    )
    expected = (
        intent_id,
        active_link_digest,
        seal_digest,
        decision_path,
        active_link_digest,
        "published",
    )
    if actual != expected:
        raise QueueOperationError("semantic_decision_conflict")


def decision_sealer(
    database: sqlite3.Connection, row: sqlite3.Row, intent_id: str, stage: str
) -> sqlite3.Row:
    """The seal of the task that published this decision, proven by its own digest."""
    seal = database.execute(
        """SELECT * FROM capture_task_link_seals
           WHERE consumer_kind='semantic-decision' AND consumer_id=? AND active_digest=?""",
        (f"{intent_id}:{stage}", row["active_link_digest"]),
    ).fetchone()
    if seal is None:
        raise QueueOperationError("semantic_decision_conflict")
    expected = capture_semantic_seal_digest(
        str(seal["task_id"]), intent_id, stage, str(row["active_link_digest"])
    )
    if seal["seal_digest"] != expected:
        raise QueueOperationError("semantic_decision_conflict")
    return seal


def redrive_lineage(database: sqlite3.Connection, task_id: str) -> tuple[str, ...]:
    """Current task through origin; corruption never means a fresh execution."""
    seen, lineage = set(), []
    current: str | None = task_id
    while current is not None:
        if current in seen:
            raise QueueOperationError("redrive_lineage_cycle")
        seen.add(current)
        lineage.append(current)
        current = _redrive_parent(database, current)
    return tuple(lineage)


def redrive_ancestors(database: sqlite3.Connection, task_id: str) -> set[str]:
    """Every ancestor after rejecting cycles and missing rows; no depth cap."""
    return set(redrive_lineage(database, task_id)[1:])


def required_redrive_row(database, task_id):
    row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise QueueOperationError("redrive_lineage_missing")
    return row


def _redrive_parent(database: sqlite3.Connection, task_id: str) -> str | None:
    row = required_redrive_row(database, task_id)
    if row["redrive_of"] is None:
        return None
    return str(row["redrive_of"])


def _compile_lineage_signature(row):
    return tuple(row[name] for name in ('kind', 'handler_version', 'input_hash'))


def _require_compile_lineage_row(database, task_id, expected):
    row = required_redrive_row(database, task_id)
    validation = validate_payload_blob(bytes(row['payload_blob']), row['input_hash'], parse=True)
    if validation.code is not None or _compile_lineage_signature(row) != expected:
        raise QueueOperationError('redrive_lineage_payload_mismatch')


def compile_lineage_origin(database, row):
    if row['kind'] != 'compile':
        raise compile_progress.ProgressConflict('only compile tasks have compile work')
    lineage = redrive_lineage(database, row['id'])
    expected = _compile_lineage_signature(row)
    for task_id in lineage:
        _require_compile_lineage_row(database, task_id, expected)
    return lineage[-1]


def decision_seal_of(
    database: sqlite3.Connection, task_id: str, intent_id: str, stage: str
) -> sqlite3.Row | None:
    """This task's seal to the decision, when its digest proves it."""
    seal = database.execute(
        """SELECT * FROM capture_task_link_seals WHERE task_id=?
           AND consumer_kind='semantic-decision' AND consumer_id=?""",
        (task_id, f"{intent_id}:{stage}"),
    ).fetchone()
    if seal is None:
        return None
    expected = capture_semantic_seal_digest(task_id, intent_id, stage, str(seal["active_digest"]))
    return seal if seal["seal_digest"] == expected else None


def require_inherited_capture_decision(
    database: sqlite3.Connection,
    sealer: sqlite3.Row,
    active: CaptureTaskBinding,
    *,
    intent_id: str,
    stage: str,
    active_link_digest: str,
) -> None:
    """A decision an ancestor of this redrive sealed for the same intent.

    The decision is one per intent and stage, but its seal and link belong to the
    task that made it; a redrive re-signs the link for the child, so the child
    could never match its parent's seal (audit 2026-09-27 B-14). A redrive keeps
    what its ancestor already decided, as a workflow redrive keeps completed steps
    (docs/research/2026-09-27-a-redrive-keeps-the-decision-its-parent-sealed.md).
    """
    own_seal = capture_semantic_seal_digest(active.task_id, intent_id, stage, active_link_digest)
    actual = (active.intent_id, active.active_digest, active.seal_digest in (None, own_seal))
    if actual != (intent_id, active_link_digest, True):
        raise QueueOperationError("semantic_decision_conflict")
    if str(sealer["task_id"]) not in redrive_ancestors(database, active.task_id):
        raise QueueOperationError("semantic_decision_conflict")

_CaptureLeaf = tuple[str, str | None, str | None, int]


def _superseded_digests(resolutions: Sequence[sqlite3.Row]) -> set[str]:
    """Every digest a later resolution replaced."""
    return {
        str(row["supersedes_digest"])
        for row in resolutions
        if row["supersedes_digest"] is not None
    }


def _resolution_leaf(
    database: sqlite3.Connection, link: sqlite3.Row, resolution: sqlite3.Row
) -> _CaptureLeaf:
    """One unsuperseded resolution, with the intent it selected if it named one."""
    digest = str(resolution["resolution_digest"])
    selected = resolution["selected_intent_id"]
    if selected is None:
        return digest, None, None, int(link["handler_version"])
    intent = database.execute(
        "SELECT * FROM capture_intents WHERE intent_id=?", (selected,)
    ).fetchone()
    if intent is None:
        raise QueueOperationError("capture_link_conflicted")
    return (
        digest,
        str(intent["intent_id"]),
        str(intent["intent_sha256"]),
        int(link["handler_version"]),
    )


def _capture_link_leaves(
    database: sqlite3.Connection,
    link: sqlite3.Row,
    resolutions: Sequence[sqlite3.Row],
) -> list[_CaptureLeaf]:
    """Everything in this link's history that nothing else superseded."""
    superseded = _superseded_digests(resolutions)
    leaves: list[_CaptureLeaf] = []
    if str(link["link_digest"]) not in superseded:
        leaves.append(
            (
                str(link["link_digest"]),
                str(link["intent_id"]),
                str(link["intent_sha256"]),
                int(link["handler_version"]),
            )
        )
    for resolution in resolutions:
        if str(resolution["resolution_digest"]) in superseded:
            continue
        leaves.append(_resolution_leaf(database, link, resolution))
    return leaves


def capture_binding_of(
    database: sqlite3.Connection, task_id: str
) -> CaptureTaskBinding:
    """The one active binding this task's link history leaves behind."""
    link = database.execute(
        "SELECT * FROM capture_task_links WHERE task_id=?", (task_id,)
    ).fetchone()
    if link is None:
        raise QueueOperationError("capture_link_conflicted")
    resolutions = database.execute(
        """SELECT * FROM capture_task_link_resolutions
           WHERE task_id=? ORDER BY created_at,resolution_digest""",
        (task_id,),
    ).fetchall()
    leaves = _capture_link_leaves(database, link, resolutions)
    if len(leaves) != 1:
        raise QueueOperationError("capture_link_conflicted")
    active_digest, intent_id, intent_sha256, handler_version = leaves[0]
    seal = database.execute(
        "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone()
    return CaptureTaskBinding(
        task_id=task_id,
        intent_id=intent_id,
        intent_sha256=intent_sha256,
        handler_version=handler_version,
        active_digest=active_digest,
        seal_digest=None if seal is None else str(seal["seal_digest"]),
    )


def require_compile_task_family(database, row, checkpoint):
    family = redrive_ancestors(database, row['id']) | {row['id']}
    if row['kind'] != 'compile' or checkpoint.task_id not in family:
        raise compile_progress.ProgressConflict('compile checkpoint belongs to another task family')


def require_uncertain_compile_dispatch(database, task_id, work_id, dispatch_digest):
    row = database.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    if (row['state'], row['blocked_capability']) not in {
            ('blocked', 'provider_outcome_unknown'), ('ready', None)}:
        raise QueueOperationError('compile_recovery_requires_unknown_outcome')
    event = compile_progress.latest_dispatch(database, work_id)
    require_dispatch_identity(event, dispatch_digest)
    require_compile_task_family(database, row, event)
    current = compile_progress.latest_event(database, work_id)
    require_compile_task_family(database, row, current)
    _require_recovery_position(event, current)
    return row


def _require_recovery_position(dispatch, current):
    if current.sequence not in (dispatch.sequence, dispatch.sequence + 1):
        raise QueueOperationError('compile_recovery_progress_advanced')


def require_dispatch_identity(event, dispatch_digest):
    if event is None:
        raise compile_progress.ProgressConflict('compile dispatch is absent')
    if (event.details.get('model_call', {}).get('phase') != 'dispatch'
            or compile_progress.event_digest(event) != dispatch_digest):
        raise compile_progress.ProgressConflict('compile dispatch changed')


def require_compile_work_family(database, row, event):
    previous = compile_progress.latest_event(database, event.work_id)
    if previous is not None:
        require_compile_task_family(database, row, previous)


def compile_operator_progress(database, task_id):
    row = database.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    origin = compile_lineage_origin(database, row)
    works = database.execute(
        """WITH RECURSIVE family(id,redrive_of) AS (
             SELECT id,redrive_of FROM tasks WHERE id=?
             UNION ALL SELECT t.id,t.redrive_of FROM tasks t
             JOIN family f ON t.id=f.redrive_of
           )
           SELECT DISTINCT e.work_id FROM compile_work_events e
           JOIN family f ON e.task_id=f.id ORDER BY e.work_id""", (task_id,),
    ).fetchall()
    return {
        'schema': 'compile-operator-progress/v1', 'task_id': task_id,
        'state': row['state'], 'answer_artifacts_checked': False,
        'history_audited': False,
        'works': [_compile_operator_work(database, item['work_id'], origin) for item in works],
    }


def _compile_operator_work(database, work_id, origin):
    event = compile_progress.latest_event(database, work_id)
    if event is None:
        raise compile_progress.ProgressCorrupt('compile work has no checkpoint')
    row = required_redrive_row(database, event.task_id)
    if compile_lineage_origin(database, row) != origin:
        raise compile_progress.ProgressCorrupt('compile work changed task family')
    return {**compile_progress.operator_summary(event),
            'recovery_dispatch': compile_progress.dispatch_summary(
                compile_progress.latest_dispatch(database, work_id))}


def require_compile_root_work(database, row, event):
    if event.sequence != 0 or 'compile_work' not in event.details:
        return
    origin = compile_lineage_origin(database, row)
    compile_progress.validate_root_event(event, origin)


def _completed_compile_attestation(database, row):
    completed = compile_progress.completed_work(database, compile_lineage_origin(database, row))
    if completed is not None:
        require_compile_task_family(database, row, completed)
    return completed


def require_complete_compile_attestation(database, row):
    try:
        completed = _completed_compile_attestation(database, row)
    except QueueOperationError as error:
        raise CompileCompletionError(error.code) from error
    except (ValueError, TypeError, KeyError) as error:
        raise CompileCompletionError('compile_completion_corrupt') from error
    if completed is None:
        raise CompileCompletionError('compile_work_incomplete')
