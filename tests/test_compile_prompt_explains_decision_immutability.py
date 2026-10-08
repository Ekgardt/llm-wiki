"""The draft receives the same immutable-decision rule its validator enforces."""
import compile_memory as compiler

from tests.test_compile_binds_the_protected_source_view import _legacy_packet


def test_decision_rule_precedes_untrusted_sources(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    prompt = compiler._draft_base_prompt(inputs)
    instruction = 'Existing pages whose YAML frontmatter type is decision are immutable.'
    assert instruction in prompt
    assert prompt.index(instruction) < prompt.index('IMMUTABLE SOURCES\n')
    assert 'Never update them or create an operation whose slug names one of them.' in prompt


def test_rule_preserves_new_decisions_and_required_knowledge(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    prompt, schema = compiler._draft_layout(inputs)
    assert 'New decisions may be created under a genuinely new slug.' in prompt
    assert 'Preserve new durable knowledge in a separate evidenced page and link the existing decision.' in prompt
    assert schema['properties']['operations']['items']['properties']['action']['enum'] == ['create', 'update']


def test_prompt_program_invalidates_pre_rule_cached_drafts():
    assert compiler.DRAFT_PROGRAM.startswith('compile-draft/v16:')
