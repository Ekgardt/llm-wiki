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

## Protected local invocation accounting, 2026-10-05

The common counter joined the protected system/schema/task strings, but Codex
later added the task frame and system/user delimiters. Its selected executable
was also located after counting. Five original regression failures reproduced
the missing final framing, a changed executable selection, and content,
replacement or disappearance of the selected file; the implicit-model control
passed. These were isolated calls with a controlled CLI seam, not model calls.

The candidate prepares one immutable local invocation after DLP. The exact
prepared string is both counted and written as Codex stdin. It includes the
schema instruction, task frame and complete Unicode data. The selected command
path, resolved target, file identity and streamed SHA-256 are retained for that
attempt and checked again immediately before launch. Dispatch does not locate
another executable. Existing explicit model and reasoning values are retained;
an implicit model remains unknown and gets no new model flag. Reported completed
usage still takes precedence over a pre-call estimate. Other provider adapters
and historical receipt schemas remain unchanged.

This check covers the selected executable file, not every interpreter,
resource, configuration file or later backend component it may use. It is not
an operating-system execution sandbox or a claim that a non-cooperating writer
cannot race the final check and process launch. No configuration-resolution or
full remote request attestation has been added.

Three independently maintained primary references were read on 2026-10-05:
[OpenAI developer commands](https://learn.chatgpt.com/docs/developer-commands)
documents the experimental prompt-input view;
[Anthropic token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)
distinguishes model-specific estimates and automatically added tokens;
[Hugging Face chat templates](https://huggingface.co/docs/transformers/main/en/chat_templating)
explains why message content alone does not represent the final token sequence.
The installed Codex 0.160.0 help and version-generated protocol were inspected
separately; API model specifications are not CLI capacity proof.

Alternatives rejected: counting a separately reconstructed payload, resolving
the executable a second time, changing the operator's model, treating a byte
estimate as a tokenizer-independent bound, or inventing an output-limit flag.
The selected file is hashed in bounded read buffers without a file-size cap or
whole-file allocation. One read-only installed-file observation measured
289,101,384 bytes, 0.546 seconds preparation and 0.842 seconds including the
second proof, 0.641 CPU seconds and 24,960 KiB process peak RSS. This is added
local cost, not a speed or token-saving claim. It did not execute the CLI.

Final related qualification: 116 tests passed in 2.67 seconds, with genuine
file-drift and symlink guards and a full large Unicode JSON payload. Thirteen
initial broader failures came from old fixtures naming nonexistent mocked
executables. Their new fixture binds a real temporary file while preserving
all existing usage, DLP, command, neutral-directory and hook assertions.
Actual changed and nested callable complexity, Ruff and Python 3.10 grammar
were checked. No provider inference was performed for this change.

The local 32,768 planning target is not removed. The 4,000 output reserve remains
a planning reserve; Codex still declares output enforcement false and the
backend default unknown. The prepared stdin is not the complete CLI bootstrap,
tool schema, model template or remote wire token sequence. Exact tokenizer,
effective configuration capacity, output/reasoning allowance and real useful
whole-cycle qualification remain separate work. This candidate closes neither
the capacity finding nor the overall audit.

A final relative-path control found one more boundary: a finder result must be
made absolute before entering the neutral working directory. Its original
candidate refusal was retained, then the same check passed after normalization.
The final combined adapter, DLP, receipt-schema and weight checks passed all
172 cases in 3.11 seconds. Earlier qualification and cost measurements are
retained with their own source hashes; they are not a model-cycle comparison.
