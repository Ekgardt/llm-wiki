"""Saving, confirming and executing one batch publication."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from functools import partial
from pathlib import Path

import compile_progress as progress
from llm_wiki import runtime_roots
from llm_wiki.compile_call_runtime import require_checkpoint
from llm_wiki.compile_controller_archive import snapshot_json
from llm_wiki.compile_controller_base import compile_identity_digest
from llm_wiki.compile_controller_preflight import (
    preflight_batch_details,
    preflight_source_records,
    save_verified_compile_artifact,
)
from llm_wiki.sqlite_deadlines import operational_deadline_active


def check_batch_context_owner(queue, lease, saved, dependencies, deadline):
    dependencies.verify()
    operational_deadline_active(deadline)
    queue.verify_compile_checkpoint_owner(lease, saved)


def publication_content(resolved):
    return {'plan': resolved.plan, 'request_action_key': resolved.action_key,
            'action_key': _publication_action_key(resolved.action_key, resolved.plan),
            'provider_budget': dict(resolved.provider_budget)}


def _publication_action_key(request_action_key, plan):
    progress.require_digest(request_action_key)
    return compile_identity_digest({'schema': 'compile-publication-action/v1',
                                     'request_action_key': request_action_key, 'plan': plan})


def _require_publication_action(content):
    fields = {'plan', 'request_action_key', 'action_key', 'provider_budget'}
    if not isinstance(content, dict) or set(content) != fields:
        raise progress.ProgressCorrupt('invalid publication content fields')
    if content['action_key'] != _publication_action_key(content['request_action_key'], content['plan']):
        raise progress.ProgressCorrupt('publication action does not bind its accepted plan')


def _publication_binding(context, batch, content, dependencies):
    return {'schema': 'compile-publication/v2',
            'context_sequence': context.sequence,
            'context_digest': progress.event_digest(context),
            'batch_digest': compile_identity_digest(preflight_batch_details(batch)),
            'content_digest': compile_identity_digest(content),
            'dependency_digest': dependencies.digest}


def require_publication_context(queue, lease, context, batch, dependencies):
    require_checkpoint(context)
    queue.verify_compile_checkpoint_owner(lease, context)
    recorded = queue.get_compile_event(context.work_id, sequence=context.sequence)
    if recorded != context or 'compile_context' not in context.details:
        raise progress.ProgressConflict('publication requires recorded batch context')
    sources = compile_identity_digest(preflight_source_records(batch.inputs))
    if sources != context.source_digest:
        raise progress.ProgressConflict('publication daily source identity changed')
    admission = queue.get_compile_event(context.work_id, sequence=0)
    _require_publication_dependencies(admission, dependencies)


def _require_publication_dependencies(admission, dependencies):
    if dependencies.verify() != admission.details['compile_batch']['dependency_digest']:
        raise progress.ProgressConflict('publication dependency identity changed')


def _publication_checkpoint(queue, lease, context, binding):
    latest = compile_work_cursor(queue, context.work_id)
    queue.verify_compile_checkpoint_owner(lease, latest)
    if latest.stage == 'publication_ready':
        return _existing_publication_checkpoint(latest, binding)
    require_checkpoint(latest)
    if latest.details['artifact'] != context.details['artifact']:
        raise progress.ProgressConflict('publication cursor lost its context artifact')
    return replace(context, task_id=lease.id, sequence=latest.sequence + 1,
                   stage='publication_ready', details={'publication': binding})


def compile_work_cursor(queue, work_id):
    latest = queue.get_compile_event(work_id)
    if latest is None:
        raise progress.ProgressConflict('compile work is not recorded')
    if latest.stage != 'published':
        return latest
    return _completed_compile_parent(queue, latest)


def _completed_compile_parent(queue, latest):
    parent = queue.get_compile_event(latest.work_id, sequence=latest.sequence - 1)
    if parent is None or _completion_parent_digest(latest) != progress.event_digest(parent):
        raise progress.ProgressCorrupt('completed compile parent identity changed')
    return parent


def _completion_parent_digest(event):
    fields = {'publication_completion': 'publication_digest', 'repair_completion': 'repair_digest',
              'work_completion': 'preflight_digest'}
    if len(event.details) != 1:
        raise progress.ProgressCorrupt('invalid compile completion fields')
    kind = next(iter(event.details))
    try:
        return event.details[kind][fields[kind]]
    except (KeyError, TypeError) as error:
        raise progress.ProgressCorrupt('invalid compile completion parent') from error


def _existing_publication_checkpoint(latest, binding):
    if latest.outcome != 'ready' or latest.details.get('publication') != binding:
        raise progress.ProgressConflict('publication checkpoint identity changed')
    details = dict(latest.details)
    details.pop('artifact', None)
    return replace(latest, details=details)


def save_compile_publication(queue, lease, context, batch, resolved, *, dependencies, deadline=None):
    """Retain an accepted plan before Markdown writes; this is not a commit receipt."""
    from llm_wiki.compile_pipeline_receipts import (
        require_apply_arguments,
    )

    operational_deadline_active(deadline)
    require_publication_context(queue, lease, context, batch, dependencies)
    require_apply_arguments(resolved.plan, batch.inputs, resolved.action_key, batch, resolved.provider_budget)
    content = json.loads(snapshot_json(publication_content(resolved)))
    binding = _publication_binding(context, batch, content, dependencies)
    event = _publication_checkpoint(queue, lease, context, binding)
    produce = partial(_write_publication_document, event, content, dependencies, deadline)
    validate = partial(_validate_publication_document, event, dependencies, deadline)
    saved = save_verified_compile_artifact(queue, lease, event, produce, validate, deadline)
    check_batch_context_owner(queue, lease, saved, dependencies, deadline)
    return saved


def _write_publication_document(event, content, dependencies, deadline, output):
    from llm_wiki.compile_pipeline_receipts import (
        utc_now,
    )

    dependencies.verify()
    operational_deadline_active(deadline)
    document = {'schema': 'compile-publication-document/v2',
                'binding': event.details['publication'], 'content': content, 'completed_at': utc_now()}
    output.write(snapshot_json(document))
    dependencies.verify()
    operational_deadline_active(deadline)


def _require_publication_time(value):
    if not isinstance(value, str):
        raise progress.ProgressCorrupt('invalid publication timestamp')
    parsed = datetime.fromisoformat(value)
    if parsed.isoformat().replace('+00:00', 'Z') != value or not value.endswith('Z'):
        raise progress.ProgressCorrupt('invalid publication timestamp')


def _decoded_publication_document(event, data):
    document = json.loads(data)
    fields = {'schema', 'binding', 'content', 'completed_at'}
    if not isinstance(document, dict) or set(document) != fields:
        raise progress.ProgressCorrupt('invalid publication document fields')
    _require_publication_document_binding(event, document)
    _require_publication_action(document['content'])
    if snapshot_json(document) != data:
        raise progress.ProgressCorrupt('noncanonical publication document')
    _require_publication_time(document['completed_at'])
    return document


def _require_publication_document_binding(event, document):
    expected = event.details['publication']
    if (document['schema'], document['binding']) != ('compile-publication-document/v2', expected):
        raise progress.ProgressCorrupt('publication document identity changed')
    if compile_identity_digest(document['content']) != expected['content_digest']:
        raise progress.ProgressCorrupt('publication content identity changed')


def _validate_publication_document(event, dependencies, deadline, handle):
    _decoded_publication_document(event, handle.read())
    dependencies.verify()
    operational_deadline_active(deadline)


def read_compile_publication(queue, lease, event, *, deadline=None):
    """Read a verified intent; callers still require source/transaction receipts."""
    queue.verify_compile_checkpoint_owner(lease, event)
    recorded = queue.get_compile_event(event.work_id, sequence=event.sequence)
    if recorded != event or (event.stage, event.outcome) != ('publication_ready', 'ready'):
        raise progress.ProgressConflict('publication requires a recorded publication checkpoint')
    with queue.open_compile_artifact(event, deadline=deadline) as handle:
        document = _decoded_publication_document(event, handle.read())
    queue.verify_compile_checkpoint_owner(lease, event)
    operational_deadline_active(deadline)
    return {**document['content'], 'completed_at': document['completed_at']}


def _require_publication_completion_inputs(queue, lease, publication, batch, document, dependencies):
    binding = publication.details['publication']
    context = queue.get_compile_event(publication.work_id, sequence=binding['context_sequence'])
    require_publication_context(queue, lease, context, batch, dependencies)
    content = {key: document[key] for key in ('plan', 'request_action_key', 'action_key', 'provider_budget')}
    if _publication_binding(context, batch, content, dependencies) != binding:
        raise progress.ProgressConflict('publication completion input identity changed')


def require_publication_coordinator(queue, coordinator):
    expected = (queue.vault, queue.vault, queue.state_root, queue.coordinator_path)
    actual = (runtime_roots.vault_root(), coordinator.vault, coordinator.state_root, coordinator.database_path)
    if None in expected:
        raise progress.ProgressConflict('publication coordinator identity requires an adopted queue')
    if tuple(Path(path).resolve(strict=True) for path in expected) != tuple(
            Path(path).resolve(strict=True) for path in actual):
        raise progress.ProgressConflict('publication coordinator identity differs from queue')


def confirm_compile_publication(queue, lease, publication, batch, *, coordinator, dependencies,
                                owner=None, deadline=None, cancelled=None):
    """Journal source completion only after receipt/transaction proof under the writer gate.

    None means no complete publication proof; it never finishes the whole queue task.
    A prior completion is revalidated, rather than used as a deletion permit.
    """
    from llm_wiki.compile_pipeline_receipts import (
        compile_writer_gate,
    )

    require_publication_coordinator(queue, coordinator)
    compiler_deadline = float('inf') if deadline is None else deadline
    with compile_writer_gate(coordinator, owner, compiler_deadline, cancelled):
        return confirm_publication_under_gate(queue, lease, publication, batch,
                                                coordinator, dependencies, deadline, cancelled)


def confirm_publication_under_gate(queue, lease, publication, batch, coordinator, dependencies, deadline, cancelled):
    from llm_wiki.compile_pipeline_apply import (
        compile_batch_completion,
    )

    document = read_compile_publication(queue, lease, publication, deadline=deadline)
    _require_publication_completion_inputs(queue, lease, publication, batch, document, dependencies)
    compiler_deadline = float('inf') if deadline is None else deadline
    result = compile_batch_completion(batch, coordinator, deadline=compiler_deadline, cancelled=cancelled)
    check_batch_context_owner(queue, lease, publication, dependencies, deadline)
    return _record_publication_completion(queue, lease, publication, document, result)


def execute_compile_publication(queue, lease, publication, batch, *, coordinator, dependencies,
                                trigger, owner=None, deadline=None, cancelled=None):
    """Publish the recorded intent, recovering committed receipts before any write.

    Returns the verified completion event or None when publication has not produced
    complete source receipts. Neither outcome finishes the enclosing queue task.
    """
    from llm_wiki.compile_pipeline_apply import (
        apply_compile_plan,
    )

    confirm = partial(confirm_compile_publication, queue, lease, publication, batch,
                      coordinator=coordinator, dependencies=dependencies, owner=owner,
                      deadline=deadline, cancelled=cancelled)
    completed = confirm()
    if completed is not None:
        return completed
    document = read_compile_publication(queue, lease, publication, deadline=deadline)
    active = partial(_publication_cancelled, queue, lease, publication, dependencies, deadline, cancelled)
    apply_compile_plan(batch.inputs, document['plan'], action_key=document['action_key'],
                       trigger=trigger, coordinator=coordinator, batch=batch,
                       provider_budget=document['provider_budget'], completed_at=document['completed_at'],
                       owner=owner, deadline=float('inf') if deadline is None else deadline, cancelled=active)
    return confirm()


def _publication_cancelled(queue, lease, publication, dependencies, deadline, cancelled):
    check_batch_context_owner(queue, lease, publication, dependencies, deadline)
    return cancelled is not None and cancelled()


def _publication_completion_event(lease, publication, result):
    proof = {'schema': 'compile-publication-completion/v1',
             'publication_digest': progress.event_digest(publication),
             'transaction_id': result.transaction_id, 'operation_id': result.operation_id,
             'commit_sequence': result.commit_sequence, 'committed_at': result.committed_at,
             'action_key': result.action_key}
    return replace(publication, task_id=lease.id, sequence=publication.sequence + 1,
                   stage='published', outcome='completed', details={'publication_completion': proof})


def _record_publication_completion(queue, lease, publication, document, result):
    previous = queue.get_compile_event(publication.work_id, sequence=publication.sequence + 1)
    if result is None:
        return _missing_publication_receipts(previous)
    if result.action_key != document['action_key']:
        raise progress.ProgressConflict('receipts belong to another publication action')
    expected = _publication_completion_event(lease, publication, result)
    return append_or_match_compile_completion(queue, lease, previous, expected)


def _missing_publication_receipts(previous):
    if previous is not None:
        raise progress.ProgressCorrupt('recorded publication completion has no complete source receipts')
    return None


def append_or_match_compile_completion(queue, lease, previous, expected):
    if previous is None:
        queue.record_compile_event(lease, expected)
        return expected
    queue.verify_compile_checkpoint_owner(lease, previous)
    if replace(previous, task_id=lease.id) != expected:
        raise progress.ProgressConflict('compile completion authority changed')
    return previous
