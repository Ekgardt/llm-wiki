"""Real draft and critic layouts reuse parsing, never live source authority."""
import compile_memory as compiler
import evidence_resolver
import fact_keys
import pytest

from tests.test_compile_critic_preserves_native_claim_origin import _native_operation
from tests.test_compile_native_journal_parse_belongs_to_the_immutable_source import _two_units
from tests.test_critic_sizing_owns_immutable_native_parsing import _observe_parse


def _draft(inputs, operation):
    return compiler._draft_layout(inputs)


def _critic(inputs, operation):
    return compiler._critique_prompt(inputs, [operation])


def _original_draft(inputs, operation):
    with compiler._layout_protection_scope():
        return compiler._protected_draft_layout(inputs)


def _original_critic(inputs, operation):
    original = getattr(compiler, '_bound_critique_prompt', compiler._critique_prompt)
    return original(inputs, [operation])


ROUTES = {'draft': _draft, 'critic': _critic}
ORIGINAL_ROUTES = {'draft': _original_draft, 'critic': _original_critic}
ENTRY_PARSES = {'draft': 0, 'critic': 1}


@pytest.mark.parametrize('route', ROUTES)
def test_emitted_layout_parses_each_held_source_once(tmp_path, monkeypatch, route):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    expected = ORIGINAL_ROUTES[route](inputs, operation)
    journals = _observe_parse(fact_keys, '_native_journal_index', monkeypatch)
    entries = _observe_parse(evidence_resolver, 'daily_entries', monkeypatch)

    assert ROUTES[route](inputs, operation) == expected
    assert len(journals) == 1
    assert len(entries) == ENTRY_PARSES[route]
    assert compiler._SOURCE_CHOICE_PARSING.get() is None
    assert compiler._SOURCE_CHOICE_RESOLVER.get() is None

    assert ROUTES[route](inputs, operation) == expected
    assert len(journals) == 2
    assert len(entries) == 2 * ENTRY_PARSES[route]


@pytest.mark.parametrize('route', ROUTES)
def test_independent_layout_rechecks_a_changed_permanent_head(tmp_path, monkeypatch, route):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    ROUTES[route](inputs, operation)
    head = next(tmp_path.glob('knowledge/raw/sessions/**/*.breadcrumb.md'))
    head.write_bytes(head.read_bytes() + b'changed\n')

    with pytest.raises(ValueError):
        ROUTES[route](inputs, operation)

    assert compiler._SOURCE_CHOICE_PARSING.get() is None
    assert compiler._SOURCE_CHOICE_RESOLVER.get() is None


def test_critic_rechecks_a_head_changed_after_its_first_validation(tmp_path, monkeypatch):
    inputs, operation = _native_operation(tmp_path, monkeypatch)
    head = next(tmp_path.glob('knowledge/raw/sessions/**/*.breadcrumb.md'))
    original = compiler._verified_native_frame
    calls = []

    def changed(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            head.write_bytes(head.read_bytes() + b'changed\n')
        return original(*args, **kwargs)

    monkeypatch.setattr(compiler, '_verified_native_frame', changed)

    with pytest.raises(ValueError):
        _critic(inputs, operation)

    assert len(calls) == 2
    assert compiler._SOURCE_CHOICE_PARSING.get() is None
    assert compiler._SOURCE_CHOICE_RESOLVER.get() is None


def test_one_draft_with_two_native_frames_parses_the_physical_journal_once(tmp_path, monkeypatch):
    inputs, _keys = _two_units(tmp_path, monkeypatch)
    inputs = compiler._subset_compile_inputs(inputs, {part.part_key for part in inputs.dailies})
    expected = _original_draft(inputs, None)
    journals = _observe_parse(fact_keys, '_native_journal_index', monkeypatch)

    assert _draft(inputs, None) == expected
    assert len(journals) == 1
    assert compiler._SOURCE_CHOICE_PARSING.get() is None
