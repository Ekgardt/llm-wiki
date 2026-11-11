"""All compile roles judge durable information against the same explicit rubric."""
from llm_wiki import compile_pipeline_base, compile_pipeline_operations, compile_pipeline_prompts

from tests.compile_critic_drop_cannot_complete_source_work_helpers import _inputs, _proposed
from tests.the_compile_decides_what_the_snapshot_already_knows_helpers import vault as vault


def test_draft_and_both_reviews_share_the_durable_knowledge_contract(vault):
    root, _state = vault
    inputs = _inputs(root)
    prompts = (compile_pipeline_operations.draft_base_prompt(inputs), compile_pipeline_prompts.critique_prompt(inputs, [_proposed()]),
               compile_pipeline_prompts.source_review_prompt(inputs, [_proposed()]))
    for prompt in prompts:
        assert compile_pipeline_base.DURABLE_KNOWLEDGE_POLICY in prompt
        assert 'Generic instructions to continue' in prompt
        assert 'explicit owner and responsibility' in prompt
        assert 'Do not invent a requirement' in prompt
