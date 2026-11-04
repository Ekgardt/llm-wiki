# Reference filtering must preserve Python spelling rules

2026-11-04; discovered while qualifying value_references on351f5da7.
The current byte-substring prefilter is unsound: `lookup="\u0066oo"` loads the
string identifier foo, and `lookup=K` loads normalized identifier K. The full
reference index reports both; a narrowed index skips each source and reports no
reference. Adjacent literals can also form an identifier absent from raw bytes.
Reproduction: logs/value-reference-prefilter-reproduction.json. This is an
existing correctness defect, not a behavior change caused by code extraction.

Research checked [Python lexical rules](https://docs.python.org/3.14/reference/lexical_analysis.html),
[Python tokenize](https://docs.python.org/3.14/library/tokenize.html),
[Unicode identifier normalization](https://www.unicode.org/reports/tr31/),
[PEP3131](https://peps.python.org/pep-3131/) and
[Vulture's analysis limits](https://github.com/jendrikseipp/vulture).
The runtime remains the approved Python3.14 series. The installed interpreter's
Unicode database defines normalization; this work does not substitute a new
Unicode release. Tokenization honors source encodings and exposes literal
spelling; ast.literal_eval decodes literals without executing arbitrary code.
For malformed input, tokenizer behavior is not guaranteed: errors must fall back
to the existing full parser/lexical path, never become evidence of absence.

Keep raw-byte hits as a fast positive. For misses, inspect standard Python tokens:
normalize NAME tokens with NFKC, decode STRING literals and interpolated-string
middle tokens with the original quote/raw-prefix context, and conservatively
join literal pieces. Joining unrelated strings may admit extra files; only the
existing full AST reader decides actual references. The token filter cannot
claim a reference itself. Empty requested-name sets still skip all sources.
Nested f/t-string context is a stack; tokenization/literal errors defer to full
analysis. No hand-written identifier grammar, new dependency, cache, schema,
process, numeric cap or changed public verdict is introduced.

Alternatives measured on the same268 scripts, query _architecture_symbol:
full analysis2.89s, old incorrect byte filter0.083s. A conservative tokenizer that
falls back for every f/t-string takes3.01s and parses205 files; reject it. Keeping
standard-token quote context takes1.49s and parses the same2 files as the old
filter. It is still materially slower than the incorrect shortcut; do not claim
unchanged latency or model-token savings. These are exploratory single runs,
not a statistically qualified benchmark. Final measurements must use the actual
implementation and exact same sources/query. Caching would require a separate
correct dependency/compiler invalidation contract and is not added here.

Prototype parity found no false negatives in16 synthetic escaped, concatenated,
normalized, nested f-string, t-string, encoding-cookie and malformed cases.
The268-script corpus contains no names transformed away from their raw spelling,
so its matching result does not exercise the defect. Regression tests must keep
the constructed cases and also prove that a conservative token hit alone does
not turn an unused name into a claimed reference. Original full-index semantics,
dispatch checks, scoped refusal and integration assertions remain mandatory.
No module155, completed repair or native qualification is claimed by this plan.

Implementation evidence: the original source fails15 new regressions, with6
passing controls. The repaired reader passes81 focused/reference/graph integration
checks. A later prefix/quote matrix initially failed12 cases because the test
mixed t-string and ordinary-string implicit concatenation, which Python rejects.
Corrected those inputs to concatenate two t-strings; production code did not
change for that test correction. All58 scoped-reader cases pass, including
malformed-tokenizer fallback and conservative false-positive controls.

Three interleaved measurements on the same268 source bytes: for
_architecture_symbol, medians are2.939s full,0.088s old byte-only and1.491s fixed;
for an absent symbol,2.923s,0.0036s and1.480s respectively. The repaired reader
retains2 and0 full AST source reads for these queries. It trades the incorrect
shortcut's latency for sound Python spelling analysis; it is approximately half
the full traversal time in this stand, not an unchanged-cost optimization.
Two preliminary regex gates did not offer a substantial measured improvement
and add spelling-proof obligations; neither is included in production. Their
prototype labels “original-narrow” refer to the already repaired working reader,
not to the historical byte-only baseline. The actual timing report explicitly
loads HEAD's unchanged baseline and records every source hash.

Actual complexity/shape:17 changed functions, all within the mandatory limits;
Ruff passed. Evidence: logs/value-reference-spelling-*,
value-reference-prefilter-actual-timing.json and
value-reference-tstring-test-assumption.json. Whole-project/native qualification
is tracked separately; this correction is not completion of decomposition.

Expanded checkout qualification:3595 passed,3 platform skips,one pre-existing
pytest iterator warning in138.92seconds. Two explicit verdict regressions also
prove that encoded/concatenated references receive referenced_without_call, not
the unsupported unused verdict. No original assertion or guard was weakened.
