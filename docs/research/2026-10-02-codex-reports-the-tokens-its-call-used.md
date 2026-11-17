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


## 2026-10-05: packing and dispatch share the local provider layout

The installed planner omitted text that the existing Codex serializer actually
puts into stdin: the schema instruction, task frame and SYSTEM/USER separators.
On one immutable, genuine closed 2026-09-23 day (26,871 bytes, two parts), the
old 32,768 target reported 27,624 and 27,742 input units, while the protected
local stdin contained 27,929 and 28,047 UTF-8 bytes. Both exceeded the existing
27,744 available input allowance. No inference was needed to reproduce this.
A separate original-API control also showed DLP policy drift: the old fit passed,
then the actual prepared payload grew from 273 to 290 bytes and was dispatched
once through an intercepted CLI. This is a local planning/dispatch defect,
not proof that a backend context window was exceeded.

The candidate uses the existing provider-mode and serialization functions in
llm_client. Before provider selection, byte packing covers the largest local
layout among the configured forced/fallback candidates; a tokenizer counts each
layout separately and takes the largest count, including the final packing
manifest. No provider probe, executable lookup or binary hashing occurs in these
measurements. The byte-additive path keeps its existing owned-input projection.
For a chosen provider, the final fit uses its protected layout. An optional
internal input budget is checked against the actual prepared Codex count before
invocation, so a later DLP change cannot silently send a known oversized input.
Absent batches, other providers and opaque custom backends retain their previous
contracts. Unknown counts do not acquire a new library-wide refusal policy.
The existing output reserve remains a planning reserve, not a Codex output cap.

The same captured sources and parts were used in both no-model packing arms.
At the existing target the candidate's local stdin was 27,691 and 27,736 bytes;
both fit, and their measured counts matched those protected strings. The
experimental 258,400 target produced one request: measured 253,080, protected
253,053 after DLP. That difference is explicitly retained; unprotected planning
is an estimate, not a guarantee that redaction never expands text. The dispatch
check is still required. All original parts were preserved exactly once.
Optional context changed only through the existing fit selection. The large
experimental target still fills substantially more context; fewer batches do
not prove full-cycle token efficiency. Neither target nor configuration was
changed by this correction.

The matched timings are observations, not isolated speed claims: old/new 32,768
packing took 1.456/1.187 seconds, CPU 1.442/1.078 seconds, peak RSS
59,604/61,592 KiB. The old/new experimental arm took 1.531/1.307 seconds,
CPU 1.475/1.306 seconds, peak RSS 60,884/63,384 KiB. There were zero model calls.
A later replay reconstructed every selected context by SHA and verified the
exact protected stdin digests. Its checker is the NEW candidate implementation,
including when checking OLD packed requests: old requests fail the new final-fit
check; this must not be mistaken for the old checker's behavior. The full
available-source list had meanwhile drifted; that initial replay refusal is
retained and no identical full-list claim is made about the later replay.

The first focused controls were 2 failures, followed by the dispatch API controls
(2 failures/3 passes before the optional argument existed). The original-API
control above provides the behavioral cause independently of that API error.
The first broader run recorded 135 passes/7 failures because Ruff removed the
existing count_tokens export; the explicit used re-export was restored without
changing old tests. Two CCN-6 helpers were rejected and split before acceptance.
The resulting related suite passed 441 tests; the new eight-test module also
passed with a retained JUnit report and its measured weight. No Root installation,
model cycle, backend full-wire capacity or audit completion is claimed here.

Sources checked on 2026-10-05, independently maintained primary documentation:
[OpenAI Codex app-server](https://learn.chatgpt.com/docs/app-server),
[Hugging Face chat templates](https://huggingface.co/docs/transformers/main/en/chat_templating),
and [Anthropic token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting).
The installed CLI is 0.160.0; remote documentation is current documentation,
not a version-pinned guarantee about that binary. The first documents the
separation between thread/config initialization and turn submission; the second
explains that formatting/control tokens are part of model input; the third
explicitly treats counts as estimates that may include provider-added material.
Actual local serializer and DLP bytes establish this narrow correction.
Alternatives rejected were copied framing in the compiler, arbitrary slack,
raising the window, and declaring HTTP wire size from local message content.
Full CLI bootstrap and provider tokenization remain separate qualification work.

### 2026-10-05: attempt-owned native Codex planning evidence (candidate)

The llm-only candidate resolves the existing Codex selection before its caller
packs input. It launches the already selected executable with the same reasoning,
hooks, neutral directory and read-only settings as the existing provider path.
Native `initialize`, `config/read`, ephemeral `thread/start` and
`thread/unsubscribe` resolve the configured/default model without `turn/start`.
The same resolved model becomes a transient existing descriptor `model` value;
preparation retains that executable and passes that model through the existing
`-m` option. No persistent model selection or new setting is introduced.

Research checked on 2026-10-05: the pinned
[Codex 0.160 exec implementation](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/exec/src/lib.rs)
uses the same in-process app-server thread bootstrap; its
[config state](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/config/src/state.rs)
exposes loaded layer values and versions. The
[Python 3.10 subprocess contract](https://docs.python.org/3.10/library/subprocess.html)
requires pipe/process ownership and explicit timeout cleanup.
[Git's racy-file analysis](https://git-scm.com/docs/racy-git) explains why file
metadata alone does not prove unchanged bytes. These are independent primary
sources from OpenAI, Python and Git, rather than three accounts of one mechanism.

The chosen internal object owns native model/provider observations, exact
executable identity and SHA, current configuration-file digests, an environment
hash and the actual catalog digest. Loaded TOML values are compared with native
layer values: a second JSON serialization would introduce Rust/Python float
spelling differences. Preparation and dispatch recheck executable, files and
environment. Descriptor configuration changes are refused; fallback provenance
alone remains permitted. The object is excluded from the existing canonical
JSON descriptor. Every other provider and ordinary Codex calls without this
optional object retain their existing behavior.

The local native catalog for the observed model advertises 272000 with 95%
effective context, giving a planning estimate of 258400. This is qualified only
for the observed pinned 0.160 interface/provider/catalog. Unknown advertisements,
managed layers, profiles, configured context overrides and unsupported TOML
projection values return unknown capacity. They do not invent a model, an output
cap or a window for another backend. The unchanged CLI token contract remains
`max_tokens_enforced=False` and `backend_default`. This estimate does not count
all CLI bootstrap/wire tokens or guarantee backend acceptance.

Alternatives were leaving the configured/default model unknown, treating the
catalog's default as an independently selected model, or using EOF-only process
communication. The first cannot ground this planning change; the second can
change the user's selection. An actual EOF-only probe exited successfully without
a thread reply, so it was rejected. Interactive native reply handling uses the
existing process-tree ownership and cleanup contract. Its cost and ordinary CLI
cache/telemetry side effects remain part of qualification; no inference or user
payload is needed to resolve the basis.

Qualification: the original preparation body repeated executable discovery after
planning (one failing regression). The candidate retains the selected executable.
The final related suite passed 223 tests in 24.17 seconds. New controls cover
configuration/executable/environment/descriptor drift, missing capacity proof,
explicit-model disagreement, Unicode config values, deadline expiry and actual
owned protocol-process cleanup. An intermediate new test incorrectly called a
nonexistent descriptor method (25 passed, one failure); it was corrected to the
existing canonical serializer without changing its assertions. Earlier complexity
checks rejected CCN 7 and 6 helpers before qualification; the final actual
changed/nested/lambda analysis is at most 5, two `if`s and two levels.

The final native candidate probe resolved the existing default to the same model
and the advertised 258400 estimate in 2.589 seconds, without a turn or inference.
The prior candidate probe took 2.781 seconds; both proofs and source hashes are
retained. These observations are not an isolated latency comparison. Local
bootstrap CPU/RSS are recorded; inference tokens and cost are inapplicable to
these zero-turn probes. This component does not change the compiler's 32768 target,
expand optional context, or establish useful full-cycle quality. Caller integration,
provider-specific fallback replanning, bounded atomic capacity and a meaningful
paired native compile/retrieval cycle remain separate qualification work.

### Optional discovery compatibility, 2026-10-05

Discovery is optional for historically supported implicit CLI execution. The
owned executable's version is checked before native bootstrap. Versions whose
bootstrap has not been qualified retain the implicit execution path and an
unknown planning basis; they do not start an app-server. The qualified 0.160.0
path treats only a well-formed native method-not-found error (-32601) as an
unavailable optional capability. Invalid parameters, malformed responses,
identity/configuration drift, deadline expiry and unverified cleanup remain
visible failures. A telemetry label or arbitrary exception is not capability
evidence.

For the optional `debug models` command, only the pinned CLI parser's explicit
unsupported-subcommand category with exit status 2 permits unknown capacity.
Other failed exits and corrupt successful output remain failures. If the catalog
is unavailable, the already resolved model, executable and configuration remain
bound to the attempt, while the advertised window is unknown. All loaded file
layers are checked independently, including those after an unknown managed layer.
The comparison verifies parsed TOML/native values and the native version digest's
shape; it does not recreate Rust's serialized version hash. Physical configuration
file digests are still checked before dispatch.

The alternatives were rejecting every unavailable optional method, which breaks
working older CLI calls, and swallowing every discovery error, which would hide
unsafe drift. The selected typed capability boundary preserves both compatibility
and fail-closed identity checks. It adds no settings, persisted fields or model
selection. Exact parser wording is qualified only for this pinned implementation;
an unfamiliar response remains a failure until its category is established.

The design uses the [JSON-RPC 2.0 error contract](https://www.jsonrpc.org/specification),
[Python 3.10 subprocess lifecycle](https://docs.python.org/3.10/library/subprocess.html),
and [Git's explanation of racy file identity](https://git-scm.com/docs/racy-git),
checked on 2026-10-05. The Codex native protocol omits the JSON-RPC version member;
this change does not impose one. The pinned
[Codex 0.160.0 Cargo lock](https://github.com/openai/codex/blob/rust-v0.160.0/codex-rs/Cargo.lock)
names Clap 4.5.58. A local invalid-subcommand probe confirmed its parser category
and exit status without inference; current Clap documentation alone was not used
to claim the pinned behavior.

The frozen first candidate produced three failing compatibility controls and six
passing negative controls. A separate original control reached the actual parser
exit through the first candidate's unchanged command path and failed with
`ProviderExited`; it did not rely on an unsupported new keyword. The expanded
final related suite passed 244 tests. App-server exit failure after a valid
method-not-found reply remains a failure after verified process cleanup.
The initial failure log remains retained; none of the earlier test assertions was
weakened. This fixes optional-discovery compatibility, not the compiler's context
target, full wire token accounting, or the remaining useful complete-cycle work.
