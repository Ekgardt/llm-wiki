# A phrase is dated once, and only the user is read

Dated 2026-09-17. Found by the third audit (retrieval, M8 and M9); the research before the fix.

Files: `scripts/temporal_anchor.py`, `tests/test_a_phrase_is_dated_once_and_only_the_user_is_read.py`

## What was found

- `temporal_anchor` resolves "the day before yesterday" with one pattern and "yesterday"
  with another, and runs both over the same text. The second pattern also matches the
  `yesterday` inside the first phrase. For an entry captured on 2026-09-17 the single
  sentence "the day before yesterday I packed" produced two calendar lines, 2026-09-15 and
  2026-09-16, each quoting the same sentence. `flush_memory._dated_block` appends those lines
  to the daily entry, where they are citable text: the second one is a false dated fact. The
  same wrong date joined the search query through `query_with_dates`.
- The module's rule is "only the user's turns". `spoken_by_the_user` decides whether a text
  is a conversation with `_TURN_RE.search(text)`, and `_TURN_RE` begins with `^` but is
  compiled without `re.MULTILINE`, so it can match only at the very start of the text. An
  entry starts with its heading. An entry that holds only `**assistant:**` turns therefore
  counted as "not a conversation", was read whole, and the assistant's "yesterday" became a
  dated line — the exact case the rule was written for.

## Practice on this date

- Python's `re` documentation, `re.MULTILINE`: "When specified, the pattern character `'^'`
  matches at the beginning of the string and at the beginning of each line (immediately
  following each newline)"; and "By default, `'^'` matches only at the beginning of the
  string". (<https://docs.python.org/3/library/re.html>)
- The same page, `re.finditer`: "Return all non-overlapping matches of *pattern* in
  *string*" and "The *string* is scanned left-to-right". One alternation with the longer
  phrase first therefore consumes "the day before yesterday" whole, and the shorter word can
  no longer match inside it. That is the mechanism for overlapping phrases; removing the
  longer match from the text before a second scan would be a workaround for having two scans.

## The decision

- "the day before yesterday", "today", "yesterday" and "tomorrow" are one table of offsets
  and one pattern, the longest phrase first. `_day_before_hits` and its pattern are removed.
- `_TURN_RE` is compiled with `re.MULTILINE`, so a turn marker at the start of any line makes
  the text a conversation. `_turn_role` matches one line at a time and is unaffected.
- No other pair of patterns in the module overlaps: a weekday needs "last", "next" or
  "this past" before it, "N weeks ago" needs a count, and "last week" needs neither.
