# A key reply is read as written, and a leg votes once

Dated 2026-09-17. Found by the third audit (retrieval, L3 and L4); the research before the fix.

Files: `scripts/fact_keys.py`, `tests/test_a_key_reply_is_read_as_written_and_a_leg_votes_once.py`

## What was found

- **L3.** The nightly keying asks a model for keys per turn and reads the reply as a JSON
  object named by turn index. `_turn_indices` accepted any all-digit name and turned it into
  an integer; `_parsed_batch` then looked the value up again under `str(index)`. A reply that
  spelled the first turn `"00"` passed the first step and raised `KeyError('0')` in the
  second, which failed the whole nightly step instead of keying the batch.
- **L4.** `fact_keys.search` promises "lexical hits and, with an encoder, dense hits, one vote
  each; a turn two legs agree on comes first". The votes were counted per returned row, and a
  leg returns one row per matching *key*. A turn with three keys that each contain one word
  of the question earned three votes from the lexical leg alone and outranked the turn whose
  single key matched the whole question. The tie-break `list(votes).index(span)` was also
  quadratic and unnecessary.

## Practice on this date

- RFC 8259, section 4: "An object structure is represented as a pair of curly brackets
  surrounding zero or more name/value pairs (or members). A name is a string." The name a
  reply used is the only name its value can be read under; a normalised copy of it is a
  different string.
- Python documentation, `sorted`: "The built-in sorted() function is guaranteed to be
  stable." A dictionary keeps insertion order, so sorting the spans by votes alone keeps the
  first leg's order among equals with no index lookup.
- Reciprocal rank fusion and its simpler relatives give each *ranking* one say per document
  (Cormack, Clarke, Büttcher, SIGIR 2009: the sum runs over rankings `r ∈ R`), which is what
  the docstring already promised.

## The decision

- `_turn_indices` returns each accepted index with the name the reply used, and
  `_parsed_batch` reads the value under that name. When two names mean one turn ("0" and
  "00") the first one written wins.
- A leg is reduced to its distinct spans, in rank order, before it votes. Two legs can give a
  span at most two votes. The order among equals is the stable sort's.
- Whether the separate keys leg stays at all is a measurement the owner has reserved
  (the 2026-09-16 note: "the loser is removed"); this fix only makes it do what it says.
