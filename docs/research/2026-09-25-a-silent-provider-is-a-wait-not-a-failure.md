# A silent provider is a wait, not a failure

Date: 2026-09-25. Audit item A-11 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code and by running it)

- `llm_client.call_llm_result` returns `None` whenever no candidate produced
  text: a provider that failed, timed out, was rate-limited or is absent. It is
  `None` in forced mode too, because a failed forced candidate is not terminal
  and the loop ends after it. Run on this machine with
  `MEMORY_LLM_PROVIDER=openai` and no key: it printed `None`.
- `flush_memory._call_capture_classifier` turned `None` into a plain
  `RuntimeError`. Only an `LLMResult` with `available=False` became
  `CaptureProviderUnavailable`, and the product never returns one outside the
  fake provider. So the hour-long stated wait (`PROVIDER_RETRY_SECONDS`) never
  engaged: eight attempts ran on the queue's own short backoff, about an hour in
  all, and the capture was lost.
- `tests/test_an_absent_provider_is_waited_for.py` fed a hand-built
  `LLMResult(available=False)` — a reply the product does not give.

## Source

- Claude API errors, https://platform.claude.com/docs/en/api/errors (fetched
  2026-09-25): "A tier spend-cap 429 has no `retry-after` header and keeps
  failing until access resumes"; 529 `overloaded_error` means "The API is
  temporarily overloaded"; the SDKs retry transient failures "twice by default".
  An outage of the provider behind a subscription is measured in hours, not in
  a short backoff, so a capture must wait for it rather than spend its attempts.

## Decision

- `_call_capture_classifier` treats "no provider answered" (`None`) as
  `CaptureProviderUnavailable`, the same as an unavailable result. A result of
  any other type stays a programming error.
- The test drives the real chain: a forced provider that cannot answer, with no
  stand-in reply.
- No change to `call_llm_result`'s contract: its many other callers read `None`
  as "no answer" already.

## Files

- `scripts/flush_memory.py`
- `tests/test_an_absent_provider_is_waited_for.py`
- `CHANGELOG.md`
