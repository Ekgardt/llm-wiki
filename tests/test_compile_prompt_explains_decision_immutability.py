"""The draft receives the same immutable-decision rule its validator enforces."""
from llm_wiki import compile_pipeline_base, compile_pipeline_operations, compile_pipeline_prompts

from tests.compile_binds_the_protected_source_view_helpers import _legacy_packet


def test_decision_rule_precedes_untrusted_sources(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    prompt = compile_pipeline_operations.draft_base_prompt(inputs)
    instruction = 'Existing pages whose YAML frontmatter type is decision are immutable.'
    assert instruction in prompt
    assert prompt.index(instruction) < prompt.index('IMMUTABLE SOURCES\n')
    assert 'Never update them or create an operation whose slug names one of them.' in prompt


def test_rule_preserves_new_decisions_and_required_knowledge(tmp_path, monkeypatch):
    inputs = _legacy_packet(tmp_path, monkeypatch, ['A durable exact fact.'])
    prompt, schema = compile_pipeline_prompts.draft_layout(inputs)
    assert 'New decisions may be created under a genuinely new slug.' in prompt
    assert 'Preserve new durable knowledge in a separate evidenced page and link the existing decision.' in prompt
    assert schema['properties']['operations']['items']['properties']['action']['enum'] == ['create', 'update']


def test_prompt_program_invalidates_pre_rule_cached_drafts():
    assert compile_pipeline_base.DRAFT_PROGRAM.startswith('compile-draft/v22:')
