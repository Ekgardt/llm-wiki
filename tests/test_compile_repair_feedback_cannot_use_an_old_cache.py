"""A cached plan cannot complete newly reviewed correction work without its feedback."""
import pytest

from tests.test_compile_critic_drop_cannot_complete_source_work import _attempt, _inputs, _proposed
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault


@pytest.mark.parametrize('has_operations', [False, True], ids=['cached-empty-plan', 'cached-reviewed-plan'])
def test_new_review_work_cannot_be_bypassed_by_a_previous_source_plan(vault, has_operations):
    import compile_memory as compiler

    root, state = vault
    operations = [_proposed()] if has_operations else []
    attempt, provider = _attempt(_inputs(root), state, operations, ['pass'])
    actions = attempt._actions(provider)
    accepted = attempt._critiqued(provider, actions, operations)
    assert accepted is not None
    assert attempt._cached(actions, provider).cache_hit
    original = attempt.cache.get(accepted.action, attempt._validator)
    reviewed = _proposed()
    reviews = [dict(slug=reviewed['slug'], verdict='drop', reason='Correct the previously unsupported scope.')]
    attempt.critic_feedback = compiler._validated_critique_feedback([reviewed], reviews)

    assert attempt._cached(actions, provider) is None

    assert attempt.critic_feedback == compiler._validated_critique_feedback([reviewed], reviews)
    assert attempt.cache.get(accepted.action, attempt._validator) == original


def test_a_repair_does_not_even_read_a_plan_for_the_old_input(vault, monkeypatch):
    root, state = vault
    operation = _proposed()
    attempt, provider = _attempt(_inputs(root), state, [operation], ['drop'])
    assert attempt._critiqued(provider, attempt._actions(provider), [operation]) is None

    def wrong_input(*_args):
        raise AssertionError('The old cache cannot decide this newly reviewed correction')

    monkeypatch.setattr(attempt.cache, 'get', wrong_input)

    assert attempt._cached(attempt._actions(provider), provider) is None
