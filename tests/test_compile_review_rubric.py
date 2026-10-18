"""All compile roles judge durable information against the same explicit rubric."""
import compile_memory as cm

from tests.test_compile_critic_drop_cannot_complete_source_work import _inputs, _proposed
from tests.test_the_compile_decides_what_the_snapshot_already_knows import vault as vault


def test_draft_and_both_reviews_share_the_durable_knowledge_contract(vault):
    root, _state = vault
    inputs = _inputs(root)
    prompts = (cm._draft_base_prompt(inputs), cm._critique_prompt(inputs, [_proposed()]),
               cm._source_review_prompt(inputs, [_proposed()]))
    for prompt in prompts:
        assert cm.DURABLE_KNOWLEDGE_POLICY in prompt
        assert 'Generic instructions to continue' in prompt
        assert 'explicit owner and responsibility' in prompt
        assert 'Do not invent a requirement' in prompt
