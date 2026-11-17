"""Original score regression, independent from the retrieval executor."""
import lane_score


def test_the_score_reads_a_silent_lane_as_slightly_for_the_candidate() -> None:
    last = lane_score.score(lexical_rank=lane_score.FURTHEST_RANK, dense_rank=5, rerank_score=None,
                            text="**assistant:** reply")
    silent = lane_score.score(lexical_rank=None, dense_rank=5, rerank_score=None, text="**assistant:** reply")

    assert silent > last
    assert lane_score.is_user_turn("**user:** mine\n**assistant:** theirs") is True
