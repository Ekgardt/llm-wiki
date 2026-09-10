# Native Codex hooks and the app-server handshake

Public source base: [`c8dda9abf6f06e17c3a8143bb1ea16c2fe872588`](https://github.com/Ekgardt/llm-wiki/tree/c8dda9abf6f06e17c3a8143bb1ea16c2fe872588).
The observations and combined regression run were made on a local vault
checkpoint whose scripts, tests, pyproject.toml and uv.lock are byte-identical
to that public base. This source comparison establishes applicability; it does
not relabel the earlier execution as a separate run on public main.

Reviewed 2026-09-09 against installed Codex CLI 0.153.4. This packages the
compatibility and transport fixes already reviewed separately. It does not
change user trust, provider selection, installation ownership, or configuration.

The [official hooks documentation](https://learn.chatgpt.com/docs/hooks) and
[advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced#hooks)
describe lifecycle hooks and trust attached to hook definitions. Installed native
output supplies camelCase event names, rendered commands and persisted hook-state
metadata. These exact observed shapes are compatibility facts for this version,
not a promise that every future CLI uses the same schema.

The validator excludes the exact `state` metadata table from event validation,
while retaining strict types for other groups. Doctor maps four exact native event
names and compares either the original template command or its narrow rendered
POSIX form. Commands are compared as text; they are never executed for comparison.
Enabled state, uniqueness and native trust verdicts remain required. It neither
trusts hooks automatically nor claims arbitrary shell commands are equivalent.

The previous app-server probe sent initialization and hooks/list together, closed
stdin immediately, and read large buffered chunks. The installed peer needs its
initialization reply consumed before subsequent requests. The repaired probe reads
available chunks, awaits response 1, sends initialized and hooks/list, awaits
response 2, then closes and reaps. It preserves deadlines and output ceilings;
protocol errors, malformed data, EOF, overflow and timeouts refuse the probe.
Cleanup is bounded. The probe sends no model turn, capture or trust mutation.

The [official app-server initialization contract](https://learn.chatgpt.com/docs/app-server#initialization)
requires initialize before other methods, followed by initialized. Waiting for
its response also validates successful initialization before the hook query;
the sequence was checked against the installed runtime and protocol responses. Real subprocess
regressions cover sequential acknowledgement, initialization refusal, timeout,
output overflow and process cleanup. Hook tests cover rendered/native definitions
and strict rejection. An observed successful native hooks probe establishes hook
availability, not completed capture or successful memory recall.

The existing installer can still regard rendered inline definitions as a conflict
with its unrendered installation template. That conservative ownership check is
preserved; native runtime availability and installer ownership are distinct facts.
