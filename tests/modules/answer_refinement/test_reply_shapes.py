"""Original query/group reply interpretation assertions, unchanged."""
import json

import aggregation_pass


def test_a_reply_that_is_not_queries_fans_out_to_nothing() -> None:
    assert aggregation_pass.fan_out_queries("How many?", ["a"], lambda prompt: "no json here") == []
    assert aggregation_pass.fan_out_queries("How many?", [], lambda prompt: '{"queries": "x"}') == []
    replies = '{"queries": ["How many?", " ", "one", "one", 3, "two"]}'
    assert aggregation_pass.fan_out_queries("How many?", [], lambda prompt: replies) == ["one", "two"]



def test_at_most_twelve_queries_are_run() -> None:
    reply = json.dumps({"queries": [f"query {index}" for index in range(20)]})
    assert len(aggregation_pass.fan_out_queries("How many?", [], lambda prompt: reply)) == 12



def test_the_passes_beside_the_answer_read_the_same_way():
    reply = "Grouping the mentions.\n" + json.dumps({"groups": [[0, 1]]})

    assert aggregation_pass.parsed_groups(reply, 2) == [[0, 1]]
