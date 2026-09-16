# A user turn always starts its own chunk

Dated 2026-09-16. A short user turn is folded into the assistant reply before it, and the
fact it states is then buried in someone else's words. The research before the fix.

## What was found

- `corpus_snapshot._long_enough_cuts` drops a turn boundary when the round it starts is
  shorter than `MIN_ROUND_BYTES = 160`, "because a bare 'thanks' is not a unit worth finding
  on its own" (2026-09-08).
- Measured on LongMemEval on 2026-09-14/16: of 896 evidence turns, 34 are shorter than that
  bound. Of the evidence turns the reader missed, the short ones missed 7 times out of 10,
  against 35 % for the rest. One example, question `118b2229`: the answer —
  "my daily commute … takes 45 minutes each way", 91 characters — sits at the end of a
  2 101-character chunk that begins with an assistant reply about note-taking apps, and the
  chunk ranked 53rd.
- The same folding also flips the role of the chunk, and role is the strongest feature of
  the order the vault now uses (`lane_score`, 2026-09-16): a chunk beginning with
  `**assistant:**` is read as an assistant turn even when it ends with the user's fact.
- 91 % of evidence turns are user turns; the value the reader needs is the round, which the
  answer path already assembles by carrying a retrieved turn with its partner
  (`query_memory._with_partner`).

## Practice on this date

- Small keys, large values (LongMemEval CP1, Dense X Retrieval): index the short,
  self-contained statement and deliver the larger unit around it. A key that cannot be
  addressed on its own is not a key.
- A boundary rule should follow the role of the text, not only its length: the reason a bare
  "thanks" is not worth a slot is that it states nothing, not that it is short — and the
  order already decides that by score.

## The decision

- A user turn always begins a chunk, whatever its length. An assistant turn keeps the
  `MIN_ROUND_BYTES` rule: a short reply still folds into the round before it.
- Nothing changes for the reader's unit: a retrieved user turn still arrives with the reply
  that follows it.

Files: `scripts/corpus_snapshot.py`,
`tests/test_a_user_turn_always_starts_its_own_chunk.py`,
`docs/research/2026-09-16-a-user-turn-always-starts-its-own-chunk.md`.
