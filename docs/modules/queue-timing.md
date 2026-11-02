# Queue timing

`llm_wiki.queue_timing` owns the existing UTC value normalization, timestamp
format/parsing, Retry-After readers and adopted queue retry-time calculation.
It imports only stdlib and reliability_defaults. It does not read the current
clock, open storage, schedule a task or change queue state. `memory_queue._utc_now`
and legacy configurable backoff remain with the queue.

The 12 original function names are direct identity aliases in memory_queue;
there are no duplicated implementations or forwarding wrappers. The existing
MemoryQueue._retry_after_seconds compatibility method delegates as before.
Internal RNG tests now patch queue_timing._RETRY_RANDOM, where it is consumed;
there is one generator and no globals redirection. Old internal constant aliases
are unnecessary: source/test search found only the migrated RNG monkeypatch.

Naive time means UTC by the existing application contract. Timestamp precision,
HTTP-date parsing, bool exclusion, seven-day bound, DEFAULTS, full jitter and
max(jitter, Retry-After) stay unchanged. Non-ready states return the same now.
The separate numeric parsing fix in 2ea7fa24 precedes this extraction and remains
covered, including unusual Unicode digits and arbitrary-size integers.

Own tests are under tests/modules/queue_timing. The context manifest includes
all tests, source dependencies, instructions and research; the ceiling is
100000 o200k_base tokens. Run `uv run pytest tests/modules/queue_timing -q -s`.
Integration additionally checks old call names, patched clocks, legacy/adopted
failure settlement, corruption handling and independent next capture work.
Those integration tests load storage intentionally and do not belong to the
standalone maintenance packet. No live rollout is implied by qualification.

Evidence: ../research/2026-11-02-queue-timing-boundary.md.
