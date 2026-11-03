"""Durable taxonomy stays distinct from staging evidence."""
import okf_types


def test_claim_candidate_is_not_a_durable_okf_type() -> None:
    from okf_types import CANONICAL_TYPES, INBOX_TYPES

    assert "claim-candidate" not in CANONICAL_TYPES
    assert INBOX_TYPES == frozenset({"claim-candidate"})



def test_taxonomy_matches_the_durable_page_contract():
    assert okf_types.CANONICAL_TYPES == frozenset({
        'debugging', 'pattern', 'raw-source', 'concept', 'decision', 'qa', 'entity',
        'synthesis', 'workflow', 'gap', 'skill', 'rule', 'project-state',
        'project-context', 'bootstrap-context',
    })
    assert okf_types.CANONICAL_TYPES.isdisjoint(okf_types.INBOX_TYPES)
    assert okf_types.NEVER_ARCHIVE_TYPES <= okf_types.CANONICAL_TYPES
    assert set(okf_types.TYPE_AGE_DAYS).isdisjoint(okf_types.NEVER_ARCHIVE_TYPES)


def test_aliases_and_retention_keep_the_declared_page_policy():
    assert okf_types.TYPE_ALIASES == {
        'comparison': 'synthesis', 'connection': 'synthesis', 'fact': 'concept',
    }
    assert set(okf_types.TYPE_ALIASES.values()) <= okf_types.CANONICAL_TYPES
    assert okf_types.TYPE_AGE_DAYS == {
        'debugging': 60, 'gap': 90, 'pattern': 180, 'workflow': 365, 'qa': 365,
    }
    assert okf_types.DEFAULT_AGE_DAYS == 180
