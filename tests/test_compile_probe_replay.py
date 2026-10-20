"""Read-only readiness observations preserve the original fallback decisions."""
from dataclasses import replace

import compile_controller as controller
import memory_queue
import pytest

from tests.test_compile_call_controller import work as work
from tests.test_compile_preflight_consumer import _dependencies
from tests.test_compile_progress_admission import upgraded as upgraded
from tests.test_the_compile_decides_what_the_snapshot_already_knows import _provider


def _replay(work):
    return controller.CompileReplay(*work, dependencies=_dependencies())


def _forbidden(*args):
    pytest.fail('retained observation must not probe again')


def test_probe_replay_preserves_unavailable_then_available_candidates(work):
    first = _provider()
    second = replace(first, model='fallback', candidate_index=1)
    replay = _replay(work)
    assert replay.probe_candidate(first, lambda _: False) is False
    assert replay.probe_candidate(second, lambda _: True) is True
    resumed = _replay(work)
    assert resumed.probe_candidate(first, _forbidden) is False
    assert resumed.probe_candidate(second, _forbidden) is True
    assert resumed.checkpoint == replay.checkpoint


def test_all_unavailable_probes_can_be_rechecked_without_any_model_dispatch(work):
    candidate = _provider()
    assert _replay(work).probe_candidate(candidate, lambda _: False) is False
    assert work[0].get_compile_event(work[2].work_id) == work[2]
    assert _replay(work).probe_candidate(candidate, lambda _: True) is True


@pytest.mark.parametrize('change', [
    {'model': 'changed'}, {'_endpoint': 'https://changed.example'},
    {'_resolution_failure': 'provider_timeout'},
])
def test_probe_replay_refuses_changed_candidate(work, change):
    original = _provider()
    assert _replay(work).probe_candidate(original, lambda _: True)
    with pytest.raises(controller.progress.ProgressConflict):
        _replay(work).probe_candidate(replace(original, **change), _forbidden)


def test_invalid_probe_result_is_not_recorded(work):
    with pytest.raises(ValueError, match='boolean'):
        _replay(work).probe_candidate(_provider(), lambda _: None)
    assert work[0].get_compile_event(work[2].work_id) == work[2]


def test_probe_event_failure_allows_only_read_only_probe_to_repeat(work, monkeypatch):
    probes = []

    def probe(_descriptor):
        probes.append(True)
        return True

    def interrupted(*args, **kwargs):
        raise OSError('probe event not committed')

    with monkeypatch.context() as patch:
        patch.setattr(work[0], 'record_compile_event', interrupted)
        with pytest.raises(OSError, match='probe event'):
            _replay(work).probe_candidate(_provider(), probe)
    assert _replay(work).probe_candidate(_provider(), probe)
    assert probes == [True, True]


def test_stale_probe_owner_is_refused_before_probe(work):
    stale = (work[0], replace(work[1], token='stale'), work[2])
    with pytest.raises(memory_queue.LeaseFenceError):
        _replay(stale).probe_candidate(_provider(), _forbidden)
