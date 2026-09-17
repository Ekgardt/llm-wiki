# "Last week" is a week

Dated 2026-09-17. Third audit, retrieval L14: "last week" resolved to a single day,
anchor − 7, and that day was written into the memory as a dated fact.

Files: `scripts/temporal_anchor.py`, `scripts/query_memory.py`,
`tests/test_last_week_is_a_week_not_a_day.py`,
`tests/test_a_question_searches_inside_its_own_dates.py`

## What was found

- `_last_week_hits` mapped the phrase "last week" to `anchor - 7 days`. That value went into
  `resolutions()` like any exact phrase, so `events()`/`annotation()` wrote a line
  `- 2023-05-24 — <the user's sentence>` into the daily entry: a dated fact, citable by the
  answerer, stating a day the user never named.
- The module's own contract forbids exactly this. Its docstring says "**Only what is stated.**
  No date is inferred, defaulted, or guessed", and the day-granularity rule says months and
  years are left alone because "two months ago" has no single correct answer. A week has no
  single correct day either.
- The same wrong value also reached the query side: `query_with_dates` added that one day as a
  search term, and `window()` built a span of one day ± 3, so "what did I do last week"
  searched the middle of the week and missed both ends.
- `window()` firing on a bare "today" is not a defect: "today" is an exact day, the dated leg
  it drives is additive (`_with_dated_leg` merges its rows, it filters nothing away), and a
  question that says "today" is asking about today.

## Practice on this date

- Temporal normalization treats granularity as part of the value: ISO 8601 identifies a week
  itself, not a day inside it — "\[Www\] is the week number prefixed by the letter W, from W01
  through W53", with "Monday" marking the start of each week, days 1 through 7 ending on
  Sunday (ISO 8601). An interval is written as its two endpoints, "Start and end, such as
  2007-03-01T13:00:00Z/2008-05-11T15:30:00Z".
- So a week-granularity expression has a correct machine value, and it is a pair of days, not
  one day. Collapsing it to a day is the loss of information that produced the false line.

## The decision

- "Last week" is the previous ISO calendar week relative to the anchor: Monday through Sunday
  of the week before the anchor's own week. It is a span, and it is kept as a span.
- Spans are separated from points. `resolutions()` keeps its meaning — exact days the user
  stated — and no longer carries "last week", so no dated line is ever written for it. The
  new `spans()` returns phrase → (first day, last day) for interval expressions.
- The query side reads both: `window()` unions the point dates (± the existing 3-day
  neighbourhood) with the spans as they are, so "last week" searches Monday to Sunday exactly;
  `query_with_dates` adds every day of a span as an ordinary term, which is what makes the
  lexical leg reach any entry of that week.
- A span is bounded by construction (7 days), and `MAX_RESOLUTIONS` still caps how many
  phrases one entry resolves.
- No behaviour of the daily annotation changes except the removal of the false line. Nothing
  here needs a stand run; the retrieval gain on temporal questions is measured when the owner
  permits a run.
