# Keep the token report beside the Codex final message

Research date: 2026-10-02. The Codex backend captured CLI stdout but discarded
it, returning only the final-message file. Its successful LLMResult therefore
lost input, output and cached-input counters. Counting prompt characters cannot
account for CLI instructions, tools, provider framing or the model's actual
output. This blocked a truthful whole-task cost comparison.

Primary sources checked today: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)
defines JSONL output and completed-turn usage; [systematic skill evaluation](https://developers.openai.com/blog/eval-skills)
describes recording actual execution and usage; [agent observability](https://developers.openai.com/api/docs/guides/agents-api/observability)
requires comparing the whole task and distinguishes unknown usage from zero.
These are independently authored capability, evaluation and accounting sources,
not three reproductions of one example. Local codex exec --help confirms --json
and --output-last-message in CLI 0.159.0. The already running native host is
0.160.0; these versions are not conflated.

The command adds --json while retaining the final-message artifact, neutral
working directory, read-only sandbox, disabled capture hooks, existing provider
identity, deadlines and process-tree cleanup. The backend returns the existing
BackendResponse type, which the common DLP/result boundary already handles for
other providers. Completed-turn reports are aggregated for each supported
counter; a missing or invalid component makes that aggregate unknown. Cached
input is a subset, carried separately and never added again to total input.
Cost and cache-write counts remain unknown when unreported. Non-success exit
status is checked before usage can be accepted. Malformed JSONL cannot be
silently accepted as a verified report.

Alternatives rejected: treating character estimates as actual usage; parsing
human stderr; starting a new persistent app-server; adding another provider or
model; disabling the user's configuration to reduce unmeasured overhead. There
is no new environment contract, runtime path, dependency, model, MCP tool,
limit or service. Reported usage is best-effort provider evidence, not a bill
or proof that a request was efficient. Capturing JSONL increases captured
stdout framing; the existing child lifetime and output checks stay in place.

The corrected pre-fix regression had 9 failures and 1 passing failure-control.
An initial fixture incorrectly used text stdout for this binary subprocess
boundary; that failed log is retained, and the corrected fixture reproduces
real bytes. The final test verifies common LLMResult counters, unknown and
invalid reports, totals across completed turns, preserved CLI failure and
output-DLP refusal. Related tests: 95 passed; additional safety group: 29
passed. Counts were obtained from separate runs, not presented as distinct
coverage without overlap.

A real candidate call through the first-party DLP boundary and installed CLI
returned the requested synthetic JSON in 5.377 seconds. It reported 14 260 input,
10 output and 12 288 cached-input tokens. This is one service/model call; the
input total already includes cached input. It used an implicit configured model
and made no request to change that model. It did not read private audit sources.
The large input overhead is evidence that short prompt estimates were not a
whole-call accounting method, not a claimed optimization. Installed proof and
paired answer quality remain separate qualification requirements.
