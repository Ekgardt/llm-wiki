"""Model calls with durable probes and checkpoints, replayed instead of repeated."""
from __future__ import annotations

from dataclasses import replace
from functools import partial

import compile_progress as progress
from llm_wiki.compile_answer_records import COMPLETED_REFUSALS
from llm_wiki.compile_call_runtime import require_checkpoint, run_model_call
from llm_wiki.compile_controller_base import compile_identity_digest
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.model_records import LLMResult


def run_candidate_call(queue, lease, checkpoint, *, dependencies, descriptor,
                       prompt, system_prompt, delivery=None, **options):
    """Use the shared DLP/preparation path, journaling only external invocation.

    The semantic caller still owns the complete work/dependency identity. The
    protected-request hash alone does not bind source snapshots or compiler code.
    """
    from llm_client import call_candidate

    dependencies.verify()
    dispatch = partial(_journal_candidate, queue, lease, checkpoint, dependencies, descriptor, delivery)
    result = call_candidate(descriptor, prompt, system_prompt, dispatch=dispatch, **options)
    dependencies.verify()
    return result


def _journal_candidate(queue, lease, checkpoint, dependencies, descriptor, delivery, prepared):
    from llm_wiki.formats.canonical import sha256_bytes

    request = sha256_bytes(canonical_json_bytes({
        'protected_request': prepared.request_digest, 'dependencies': dependencies.verify(),
    }))
    result = run_model_call(queue, lease, checkpoint, request_digest=request,
                           invoke=partial(_invoke_candidate, prepared, descriptor), delivery=delivery)
    _require_candidate_identity(result, descriptor)
    return result


def _invoke_candidate(prepared, descriptor):
    result = prepared.invoke()
    _require_candidate_identity(result, descriptor)
    return result


def _require_candidate_identity(result, descriptor):
    if not isinstance(result, LLMResult) or result.descriptor.canonical() != descriptor.canonical():
        raise ValueError('candidate result identity differs from prepared request')


class CompileReplay:
    """Replay checked call results while existing semantic validators run again.

    This cursor neither certifies publication nor invents work/dependency identity.
    Its caller supplies a verified snapshot checkpoint and a live dependency witness.
    """

    def __init__(self, queue, lease, checkpoint, *, dependencies, delivery=None):
        dependencies.verify()
        require_checkpoint(checkpoint)
        self.queue = queue
        self.lease = lease
        self.checkpoint = progress.snapshot_event(checkpoint)
        self.dependencies = dependencies
        self.delivery = delivery
        self.probe_observations = []

    def __call__(self, descriptor, prompt, system_prompt, **options):
        result = run_candidate_call(
            self.queue, self.lease, self.checkpoint, dependencies=self.dependencies,
            descriptor=descriptor, prompt=prompt, system_prompt=system_prompt, delivery=self.delivery, **options,
        )
        if _is_completed_result(result):
            self.checkpoint = _advance_replay(self.queue, self.lease, self.checkpoint)
        return result

    def probe_candidate(self, descriptor, probe):
        self.dependencies.verify()
        self.checkpoint, available = _record_or_read_compile_probe(
            self.queue, self.lease, self.checkpoint, descriptor, probe, self.probe_observations)
        self.dependencies.verify()
        return available


def _record_or_read_compile_probe(queue, lease, checkpoint, descriptor, probe, observations):
    queue.verify_compile_checkpoint_owner(lease, checkpoint)
    saved = queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 1)
    if saved is not None:
        return _read_compile_probe(queue, lease, checkpoint, saved, descriptor, observations)
    observation = _compile_probe_observation(descriptor, probe(descriptor))
    observations.append(observation)
    if not observation['available']:
        return checkpoint, False
    event = _compile_probe_event(checkpoint, lease, observations)
    recorded = record_or_read_compile_checkpoint(queue, lease, event)
    observations.clear()
    return recorded, True


def _read_compile_probe(queue, lease, checkpoint, saved, descriptor, observations):
    retained = _saved_probe_observations(saved)
    expected = _compile_probe_event(checkpoint, lease, retained)
    matched_compile_checkpoint(queue, lease, saved, expected)
    observation = _probe_observation_at(retained, len(observations))
    if _compile_probe_observation(descriptor, observation['available']) != observation:
        raise progress.ProgressConflict('retained provider probe candidate changed')
    observations.append(observation)
    if not observation['available']:
        return checkpoint, False
    observations.clear()
    return saved, True


def _probe_observation_at(observations, index):
    if index >= len(observations):
        raise progress.ProgressConflict('retained provider probe order changed')
    return observations[index]


def _saved_probe_observations(event):
    observations = event.details.get('compile_probe', {}).get('observations')
    if not isinstance(observations, list) or not observations:
        raise progress.ProgressCorrupt('missing retained provider probes')
    for observation in observations:
        _require_probe_observation(observation)
    _require_probe_selection(observations)
    return observations


def _require_probe_selection(observations):
    if [item['available'] for item in observations] != [False] * (len(observations) - 1) + [True]:
        raise progress.ProgressCorrupt('invalid retained provider probe selection')


def _require_probe_observation(observation):
    if not isinstance(observation, dict) or set(observation) != {'descriptor_digest', 'available'}:
        raise progress.ProgressCorrupt('invalid retained provider probe fields')
    available = observation['available']
    if type(available) is not bool:
        raise progress.ProgressCorrupt('invalid retained provider probe result')
    progress.require_digest(observation['descriptor_digest'])


def _compile_probe_observation(descriptor, available):
    if type(available) is not bool:
        raise ValueError('provider probe must return a boolean')
    identity = {'provider': descriptor.canonical(), 'endpoint': descriptor._endpoint,
                'resolution_failure': descriptor.resolution_failure}
    return {'descriptor_digest': compile_identity_digest(identity), 'available': available}


def _compile_probe_event(checkpoint, lease, observations):
    observation = {'schema': 'compile-provider-probe/v1',
                   'checkpoint_digest': progress.event_digest(checkpoint),
                   'observations': list(observations)}
    return replace(checkpoint, task_id=lease.id, sequence=checkpoint.sequence + 1,
                   details={'artifact': checkpoint.details['artifact'], 'compile_probe': observation})


def _advance_replay(queue, lease, checkpoint):
    answer = queue.get_compile_event(checkpoint.work_id, sequence=checkpoint.sequence + 2)
    if answer is None:
        return checkpoint  # A preflight refusal made no dispatch reservation.
    if answer.details.get('model_call', {}).get('phase') != 'answer':
        raise progress.ProgressConflict('replay requires a completed recorded call')
    queue.verify_compile_checkpoint_owner(lease, answer)
    queue.verify_compile_artifact(answer)
    following = replace(checkpoint, task_id=lease.id, sequence=answer.sequence + 1,
                        details={'artifact': checkpoint.details['artifact'],
                                 'replay': {'answer_digest': progress.event_digest(answer)}})
    return record_or_read_compile_checkpoint(queue, lease, following)


def record_or_read_compile_checkpoint(queue, lease, following):
    recorded = queue.get_compile_event(following.work_id, sequence=following.sequence)
    if recorded is None:
        queue.record_compile_event(lease, following)
        return following
    return matched_compile_checkpoint(queue, lease, recorded, following)


def matched_compile_checkpoint(queue, lease, recorded, following):
    queue.verify_compile_checkpoint_owner(lease, recorded)
    if replace(recorded, task_id=lease.id) != following:
        raise progress.ProgressConflict('compile checkpoint identity changed')
    return recorded


def _is_completed_result(result):
    return result.available is True and (
        result.text is not None or result.failure_class in COMPLETED_REFUSALS
    )
