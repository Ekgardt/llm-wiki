"""Existing memory/code/project policies remain separate and immutable."""
from llm_wiki import corpus_membership_policy as policy


def test_memory_generation_does_not_adopt_product_code_roots():
    assert policy.VAULT_CODE_ROOTS == ()
    assert policy.APPROVED_CODE_ROOTS == frozenset({'benchmark', 'docs', 'integrations', 'rules', 'scripts', 'skills', 'tests'})
    assert 'knowledge' not in policy.APPROVED_CODE_ROOTS


def test_project_claim_pages_do_not_include_the_event_journal():
    assert policy.PROJECT_FILES == frozenset({'state.md', 'context.md'})
    assert 'journal.md' not in policy.PROJECT_FILES


def test_repository_generation_and_vault_vocabulary_remain_distinct():
    assert policy.GENERATED_DIRECTORIES == frozenset({'__pycache__'})
    assert policy.VAULT_SKIP_DIRECTORIES == frozenset({'_template', 'gaps', 'raw-sources'})
    assert policy.GENERATED_DIRECTORIES.isdisjoint(policy.VAULT_SKIP_DIRECTORIES)


def test_all_existing_archive_spellings_are_preserved():
    assert policy.ARCHIVE_DIRECTORIES == frozenset({'archive', '.archive', '_archive'})
