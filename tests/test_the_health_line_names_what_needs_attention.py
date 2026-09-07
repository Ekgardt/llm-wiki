"""A health line must say which thing wants attention, not that something does.

The transactions finding printed "Transaction state requires operator
attention." and put the counts in details nobody opens. On this vault the same
nine refused attempts have stood behind that line for days — eight breadcrumb
appends and one compile the DLP boundary blocked — and a line that says the
same thing every day without saying what stops being read.

Nothing about the count, the status or the details changes; only the sentence.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doctor  # noqa: E402

STATES = {state: 0 for state in doctor.TRANSACTION_STATES}


def _details(unresolved: int = 0, invalid: bool = False) -> dict:
    return {"quarantined_unresolved": unresolved, "state_invalid": invalid}


def test_a_healthy_vault_says_so():
    assert (
        doctor._transaction_message(dict(STATES), 0, False, _details())
        == "Transaction state is healthy."
    )


def test_refused_attempts_are_named_and_counted():
    message = doctor._transaction_message(dict(STATES), 9, False, _details(9))

    assert "9 refused attempt(s) whose work never happened" in message
    assert message.endswith(".")


def test_unsettled_transactions_are_named_separately():
    states = dict(STATES, preparing=1, applying=2, conflicted=1)

    message = doctor._transaction_message(states, 4, False, _details())

    assert "4 transaction(s) still unsettled" in message
    assert "refused attempt" not in message


def test_both_kinds_are_named_together():
    states = dict(STATES, conflicted=2)

    message = doctor._transaction_message(states, 5, False, _details(3))

    assert "2 transaction(s) still unsettled" in message
    assert "3 refused attempt(s) whose work never happened" in message


def test_a_state_this_runtime_does_not_define_is_named():
    message = doctor._transaction_message(dict(STATES), 0, True, _details())

    assert "a state this runtime does not define" in message
