# A fact key keeps the complete admitted turn

Dated 2026-10-03. This change removes the unsupported 1,500-character cut in
`fact_keys._user_text`. It does not declare the original audit complete.

## Confirmed cause and scope

The collector admits physical retrieval spans bounded by its existing
`MAX_SPAN_BYTES` contract. The fact extractor then cut the selected user's
words again, at 1,500 characters, without a recorded technical or measured
quality basis. An otherwise admitted fact after that position never reached
the extractor. English, Cyrillic and emoji regressions reproduce this loss.

The fix passes every selected user segment in the admitted chunk. It preserves
the original source path, source digest, byte bounds and span digest. The
removed constant has no remaining consumer. There is no replacement limit,
new setting, dependency, runtime path or second extraction implementation.
The dated September limit inventories retain the removed name as historical
audit evidence. Existing batch, key and model-output budgets remain separate
open limit reviews; this change removes only the input-character cutoff.

## Research and alternatives

Primary references checked on 2026-10-03:

- [Python 3.10.22 JSON documentation](https://docs.python.org/3.10/library/json.html):
  retain the standard decoder and explicit errors; parsing untrusted inputs
  consumes resources. Removing a downstream text cut does not remove the
  collector's existing source and span safety boundaries.
- [RFC 8259](https://www.rfc-editor.org/rfc/rfc8259): structured source framing
  and escaping need their own treatment. Cutting an arbitrary character
  count cannot establish complete event boundaries.
- [OWASP prompt injection prevention](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html):
  captured text remains untrusted data. The existing extractor's system
  instructions and first-party model/DLP boundary are unchanged.

The installed Python is 3.12.3; the change uses existing operations compatible
with the project's Python 3.10 minimum.

Keeping or raising 1,500 preserves an unsupported cutoff. Adding another
setting would change the environment contract without establishing a useful
budget. Asking the model to summarize the tail first adds a second lossy model
step. Passing the already admitted user text is the simpler choice for this
specific defect. Its tradeoff is more input when the admitted segment exceeds
1,500 characters; no token savings are claimed.

## Verification and practical limits

Five regressions failed against the original source. After the change,
53 related tests and 52 structure/function-shape tests passed. Ruff passed.
Changed callables are checked with actual Lizard results matched to AST start
lines and the existing repository shape guard; unchanged module lambdas are
checked as well.

A paired isolated cycle uses the actual collector, key store, generation FTS
builder, search entry point and returned source content. Each arm makes one
controlled extractor call and no real model call. The source digest is equal.
The original sends 1,522 prompt bytes and produces no key or search hit for the
tail fact. The candidate sends 1,920 bytes and produces one key and one hit
whose reader content contains the complete original turn. Elapsed values
0.0784 and 0.0704 seconds are fixture measurements, not a performance claim.

Native captured JSON is a separate unresolved defect: installed inspection of
the closed 2026-10-02 daily found 460 user-prompt records, none recognized by
the legacy marker parser, and 78 frames crossing actual retrieval chunk
boundaries. This change neither parses those frames nor recovers legacy turns
whose continuation chunks have no user marker. It does not establish live
native fact extraction, model quality, overall retrieval quality or full audit
closure. Those scenarios require source-aware evidence mapping and separate
regression and useful-cycle qualification.
