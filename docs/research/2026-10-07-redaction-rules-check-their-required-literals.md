# Redaction rules check their required literals

Research date: 2026-10-07. This is an internal mechanical optimization,
not a change to provider, policy, source authority or runtime contracts.

Each certified rule carries a substring which every match necessarily contains.
Before that rule, the entire current text is checked. An absent prerequisite
proves that the unchanged regex cannot match. Otherwise the original regex runs.
Earlier substitutions can introduce later matches, so checks use the current
text at each step. Unknown rules or changed regex source/flags always run the
original regex. Case-insensitive rules use punctuation prerequisites rather
than approximating Unicode IGNORECASE with lower/casefold.

The ordered regexes, replacement strings, named-value classification, command
passwords, entropy handling and configured policy literals remain unchanged.
Input, system, schema, final appended source view, model output and publication
still pass their existing fresh boundaries. No successful scan result is cached.
The public two-field internal rule list remains available to its existing
classification consumer; a single declaration supplies regexes and prerequisites.

Primary sources checked on the research date:

* [Python 3.10 regex contracts](https://docs.python.org/3.10/library/re.html):
  ordered substitutions, capture references and Unicode case matching constrain
  equivalence.
* [Gitleaks 8.30.1 upstream rule documentation](https://github.com/gitleaks/gitleaks/blob/v8.30.1/README.md):
  necessary keyword filtering is an established pre-regex technique. That
  precedent does not certify arbitrary keywords for these Python expressions.
* [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html):
  secret protection and log integrity require preserving the security boundary.

Merged alternations risk changing rule priority and captures. Cross-context
scan caching risks stale protection. New regex engines add dependencies;
truncation or rule removal loses protection. These alternatives are rejected.

Qualification compares unchanged security tests, Unicode and replacement-order
controls, real retained request payload outputs and complete neutral packing
layouts with physical bindings. Shared-host CPU measurements are local evidence;
they do not establish full-day latency, token savings, model usefulness or healthy
completion of the audit. Literal checks add their own full-text search cost.

The original causal control failed because an absent necessary prefix still
incurred regex execution. Unknown rules, changed flags and a match introduced by
an earlier replacement remain controls, not fabricated original failures.
Actual Python 3.10 qualification passed 452 related cases, with no failures or
skips and three JUnit-property warnings. Existing tests were unchanged.
Twelve private retained prompt/system/schema payloads (578,997 UTF-8 bytes)
produced exactly equal complete outputs. Their alternating median redaction CPU
was 0.27864 seconds before and 0.15622 after, on the shared host.
Complete neutral packing of 750 rows with 12 optional context pages preserved
all selected inputs, prompts, schemas, physical bindings and protected planning
wire. Alternating CPU was 1.08490/1.09134 seconds before versus
0.98330/0.96553 after. Process RSS high-water stayed 100,088 KiB;
this is not independent per-arm peak-memory measurement.
