# A long entry is cut inside itself

Date: 2026-09-28. Scope: `evidence_resolver._daily_part_bounds`, the one splitter the
compile writer and the evidence reader share.

## What failed (measured, read-only, on the Claude-30 vault)

The nightly of 2026-09-28 19:40 failed with "daily source exceeds compile input budget",
and doctor counts 434 `compile_oversized_daily` capture failures. The splitter cut a day
only between entries (operation markers). `knowledge/daily/2026-09-27.md` (1 510 597
bytes, 5 497 entries) holds one entry of 49 964 bytes at offset 1 460 633: a single
transactional append carrying a tool line and a whole pre-compact summary (386 lines).
That entry became one part; the compile budget is 32 768 − 4 000 − 1 024 = 27 744 tokens
at the conservative one-token-per-byte prompt estimate, so `_require_daily_fits` refused
it, and because packing refuses before any dispatch, that refusal stopped the compile of
every day in the run. No other daily file on the vault has an entry longer than a part
(`MAX_DAILY_PART_BYTES`, 16 KiB); 2026-09-26 (653 332 bytes) and 2026-09-28 (754 303
bytes) split into 41 and 49 parts as before.

## Sources

- LangChain, RecursiveCharacterTextSplitter: "Recursively tries to split by different
  characters to find one that works." — try the coarsest boundary first, fall back to
  finer ones. https://reference.langchain.com/python/langchain-text-splitters/character/RecursiveCharacterTextSplitter
- Pinecone, Chunking strategies for LLM applications: "By recognizing the Markdown syntax
  (e.g., headings, lists, and code blocks), you can intelligently divide the content based
  on its structure and hierarchy, resulting in more semantically coherent chunks."; and
  "All embedding models have context windows, which determine the amount of information
  in tokens that can be processed" — a chunk that exceeds the window loses content.
  https://www.pinecone.io/learn/chunking-strategies/
- UTF-8 (Wikipedia): "it is self-synchronizing so searches for short strings or characters
  are possible; and the start of a code point can be found from a random position by
  backing up at most three bytes." — a byte cut can always be moved back onto a character
  start. https://en.wikipedia.org/wiki/UTF-8

## Decision

An entry longer than one part is cut inside itself: at the latest `## ` block start within
one part, else the latest blank line, else the latest line end, else on a UTF-8 character
boundary. Nothing is dropped or summarised: every byte stays in exactly one part, so the
compile sees the whole day and evidence spans still resolve (the reader uses the same
function). A day whose entries all fit a part is cut exactly as before, so no existing
receipt changes; on this vault only 2026-09-27 changes (92 → 95 parts, largest 16 377
bytes), and it was never compiled. Its heaviest part now measures 22 061 tokens against
27 744 available, and the whole day packs into 95 batches.

Alternatives: raise the budget (rejected — a longer entry fails again, law 9 needs a basis);
summarise an oversized entry first (rejected — a second model call and lossy, law 6);
skip the entry with a note (rejected — silent loss of the day's largest record). Trade-off:
a summary cut across parts is read in two batches; each part still carries its own
block heading when the cut falls at a block start.

## Guard

`tests/test_a_long_entry_is_cut_inside_itself.py`: a 50 KB entry leaves no part longer
than a part and loses nothing, and the day packs (both fail on the old splitter); entries
that fit are cut between entries as before; a line without breaks is cut on a character
boundary.

## Follow-up: an oversized day fails alone

Packing still refused the whole run when any one day's part did not fit: the refusal left
`pack_compile_batches` and `_run` returned before any batch, the same shape the one-bad-day
fix (audit 2026-09-27 A-3) removed for failures after packing. After the cut above no daily
part outgrows the default budget, but a smaller configured window, or a prompt that grows,
reaches the same refusal. `partition_packable` now measures each part first; a day with a
part the budget cannot take is recorded like any failed batch (`compile_oversized_daily`
plus a source failure naming the day), and the other days pack and compile as before.
`pack_compile_batches` keeps its refusal for a caller that hands it an oversized day.
Guard: `tests/test_one_oversized_day_does_not_hold_the_rest.py` — two days, a window that
holds only the smaller; on the previous code the run compiled neither.
