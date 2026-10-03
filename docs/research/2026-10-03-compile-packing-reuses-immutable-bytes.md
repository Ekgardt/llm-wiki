# Compile packing reuses immutable bytes

Research and reproduction date: 2026-10-03. Python 3.10 remains supported.
No new budgets, runtime paths, environment contracts, provider settings or
source-selection rules are introduced.

A genuine installed compile was profiled through its ordinary CLI, locks and
DLP boundary. The observed run was cooperatively interrupted after 614.006s;
603.461s belonged to packing. It made 414538 candidate measurements and no
recorded provider attempt. Repeated canonical schema serialization consumed
348.255s cumulatively, and repeated full input selection 227.448s. Times overlap
and must not be added; profiling overhead makes this diagnostic evidence rather
than an isolated performance benchmark. Another earlier interrupted run did
reach Codex, and its token usage remains unknown.

When no model is named, the existing planner estimates one token per UTF-8 byte.
The new call-local measure indexes immutable daily parts and context frames,
serializes the fixed prompt once and lazily measures each selected source frame.
The shared source renderer preserves strict UTF-8 decoding. Joining n frames
adds exactly two separator bytes between adjacent frames. Duplicate daily parts
still refuse; duplicate context frames remain present; unselected malformed
content remains unread. Real model counters still receive the entire prompt,
since token boundaries need not be additive. Final batch construction retains
its full prompt count and unchanged identity and validation.

Alternatives: stopping after the first context page that does not fit would
change selection because a later smaller page can fit; removing optional
context would sacrifice useful evidence; raising deadlines hides repeated work;
summing BPE fragment counts can change the budget. A persistent cache would need
invalidation and ownership contracts. A call-local exact byte estimate removes
the measured redundant work without those changes.

The original defect failed the new schema-reuse regression while four parity
cases passed. Nine guards now cover Unicode, whole-model counting, duplicate
parts/context, empty selections, and selected/unselected malformed bytes. Three
actual captured parts, using all 1169 sources from a 903-part daily snapshot,
produced identical old/new batches. Pair timings were 0.601/0.204, 0.631/0.327 and
0.559/0.192s; the whole read-only experiment took 2.768s. This is not completed
compilation, an isolated benchmark or full-cycle answer/token qualification.

Primary sources checked on the research date:

- [Python 3.10 profiling documentation](https://docs.python.org/3.10/library/profile.html)
  distinguishes execution profiles from benchmarks and warns about overhead.
- [RFC 3629](https://www.rfc-editor.org/rfc/rfc3629) defines UTF-8 byte sequences;
  the estimate uses actual encoded lengths rather than character counts.
- [OpenAI tiktoken](https://github.com/openai/tiktoken) describes BPE tokenization;
  byte additivity is used only for the existing byte estimate, never as a claim
  about model tokens. No tokenizer dependency or version is changed.

Source evidence: private audit logs for the interrupted profile, actual graph
navigation (fresh commit with incomplete graph explicitly reported), regression
runs and paired read-only measurement. Source inspection supplements unresolved
navigation edges. The audit remains open until runtime and answer qualification.
