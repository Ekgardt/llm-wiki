# Compile sizing reuses the same reviewed command line

The running six-day compiler still spends substantial CPU time preparing each
batch after the live-evidence parse correction. A read-only profile of an actual
refresh found repeated source-choice construction and secret redaction while
trying optional context pages. These nested costs overlap and must not be added.
The command-password pass repeatedly scans identical rows, even though this
existing pass already operates on individual `splitlines(keepends=True)` rows.

The correction reuses only that pure stage inside the existing explicitly owned
line-redaction measurement scope. Keys are the exact complete input rows,
including their terminators. The callback identity, original code object and
identities of the original three compiled regex objects must still match. A changed
rule or callback takes the original per-row path, including its errors. An
arbitrary handler cannot certify itself by copying the reviewed `pattern` and
`flags` properties. Even an equivalent recompiled object takes the original
path rather than receiving unneeded trust. The
existing scope owns the map; no cache survives its owner or is written to disk.
Outside the scope the original execution is unchanged. This introduces no new
path, environment variable, dependency, schema or numerical limit.

Full-text curl processing, entropy scanning, policy literals, allow fingerprints
and the fail-closed transport boundary still run. This is not a retained security
approval. Cross-line policy matches and changes remain checked on every input.
The dictionary trades memory for avoiding repeat work within one existing
measurement; its actual entries depend on the input, with no invented cache cap.

## Research checked on 2026-10-09

- [Python 3.10 functools](https://docs.python.org/3.10/library/functools.html)
  documents memoization, retained argument/result references, and why impure or
  stateful functions should not be cached. Python 3.10 remains the compatibility
  floor. A global `cache` or an arbitrary LRU bound was rejected; the existing
  explicit scope provides the required lifetime.
- [Python 3.10 regular-expression objects](https://docs.python.org/3.10/library/re.html#re.Pattern)
  documents the built-in compiled pattern type. The candidate's initial
  description-only guard failed a causal replacement test: copied regex metadata
  hid a different `search` method and reused an unredacted row. Requiring the
  exact original compiled objects corrects that candidate defect before installation.
- [OWASP LLM Prompt Injection Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
  supplies the security basis for keeping input validation and sensitive-data
  protection rather than treating a speed improvement as permission to bypass
  them. Its guidance does not itself prove this transformation correct; the
  original pipeline comparison and policy-change tests do.
- [OpenTelemetry profiles](https://opentelemetry.io/docs/concepts/signals/profiles/)
  distinguishes sampled resource attribution from task completion. The observed
  refresh profile identifies a measured cost, not a whole-cycle speed guarantee.
  No OpenTelemetry package or profiling service is added.

Alternative: replace all context sizing with an additive estimate. This was
rejected for this change because source IDs, schemas, candidate transports and
full-text policy protection have existing layout contracts. Caching whole
redaction results or policy decisions was also rejected because context and
policy can change. The small pure-stage reuse preserves those contracts.

## Evidence and remaining qualification

A causal test failed on the installed original because an unchanged command row
was scanned twice during two context measurements. Tests also cover changed
command and flag regexes, replacement callbacks and code objects, original
multiline/Unicode/CRLF handling, and changes to a cross-line DLP policy.

A paired real-input experiment on 2026-10-10 used one protected compiler request
and all 1,962 current notes, with identical captured input for both implementations.
The stage processed 499,638,213 bytes across candidate inputs. Input and output
digests were identical for both. Original wall/CPU time was 14.898/13.520 seconds;
corrected time was 2.200/2.067 seconds. The corrected scope retained 23,522 distinct
command rows. This comparison uses the final original-object guard, rather than
the earlier description-only prototype.
This is one-stage evidence under a running compiler, not whole-cycle or token
savings. The installed full compile, generation, retrieval, nightly health and
cross-platform qualification remain required before point 7 closes.

The complete original redaction pipeline was also compared on the same 85
captured protected requests (10,296,129 bytes). Input and output digests again
match. CPU time was 2.047 seconds original and 1.963 seconds corrected: this
different request workload does not establish a substantial whole-pipeline
speedup. The stage-level result applies to repeatedly sizing the same sources,
not every use of redaction.

Related checks passed: 145 tests, 32 Python 3.10 compatibility tests, and 19
tests including the unchanged shard-weight guard. The canonical shard planner
measured the new file's weight. Actual AST and Lizard analysis covered all 16
changed callables: maximum complexity 5, at most two if statements and two
levels of control-flow nesting. These checks do not replace the remaining
installed and whole-cycle qualification.

Source: `scripts/secret_redact.py`;
`tests/test_compile_sizing_reuses_only_pure_command_lines.py`; unchanged line,
secret and DLP tests; private `logs/step7-installed-live-parse-refresh-profile-2-20261009.txt`,
causal RED, real-input paired digests and focused-check reports.
