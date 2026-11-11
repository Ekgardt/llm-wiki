# Literal evidence search stops when ambiguity is proved

Date: 2026-10-07. Internal optimization; no model or publication qualification.

The unique-span validator collected every literal regex match before deciding whether exactly one existed. On a repeated quote, two matches already prove ambiguity. A regression iterator permits the two witnesses and rejects consuming a third. The original implementation failed this invariant; twelve result controls already passed. This is an algorithmic regression, not a test demanding one particular search API or a timing threshold.

The replacement finds the first byte occurrence, then looks for a second beginning at `first + max(len(quote), 1)`. This preserves regex nonoverlap: `aa` occurs once in `aaa`, twice in `aaaa`. Empty quotes match at every byte boundary, so only an empty block has exactly one. The unchanged error rejects absent or ambiguous quotes. Exhaustive small binary blocks/quotes and explicit NUL, UTF8, metacharacter, overlap and empty cases compare with the original escaped-regex oracle. Physical evidence, whole-line checks, hashes, policy and resolver validation remain unchanged.

Research checked on this date:

- [Python bytes.find](https://docs.python.org/3.10/library/stdtypes.html#bytes.find) and [re.finditer](https://docs.python.org/3.10/library/re.html#re.finditer) establish lowest byte position and nonoverlapping iteration semantics. These are one independent source family.
- [GNU libc memmem source](https://github.com/bminor/glibc/blob/master/string/memmem.c) illustrates dedicated literal byte search without regex parsing. This mirror is archived; it is original GNU code, not evidence of the current Python implementation or the latest glibc release. Direct GNU/sourceware page retrieval failed and was not counted as a successful read.
- [NIST's authored Two Way algorithm entry](https://xlinux.nist.gov/dads/HTML/twoWay.html) describes exact-pattern search and cites the original Crochemore–Perrin work. Its dated algorithm description is context, not a claim that bytes.find necessarily uses that algorithm on every supported interpreter. Direct original-paper PDF/ACM requests failed; they were not counted as inspected full papers.

Alternatives were streaming escaped-regex iteration stopped after its second match, and leaving the full match list. Two byte searches avoid both unnecessary match materialization and literal regex compilation. No claim of catastrophic regex backtracking is made: the pattern was escaped literal bytes. No dependency, cache, cap, model, setting, source identity or durable format changes.

Measured full preparation on the same neutral 750-row UTF8 fixture (65,925 bytes, five physical parts): ordinary fake-provider packing produced the same four batches, physical keys and exact full draft SHA. cProfile wall time was 2.060 seconds before and 1.192 seconds after; draft alone was .234/.224 seconds. `_sole_quote_offset` had 2,062 calls and cumulative .679/.019 seconds. The host also ran unrelated work, and the after arm overlapped a small related test run. These observations support removing the measured local overhead, not an isolated-host latency guarantee, real-day cost attribution or useful model-cycle efficiency claim. No model calls or receipts were produced.

Original RED: 1 failed, 12 passed. First connected controls: 64 passed. Additional resolver, source-choice, physical context, DLP, native and receipt controls: 137 passed. All pre-existing tests are byte unchanged. Actual modified/new functions including nested functions were analyzed before execution: ten callables, maximum CCN4, if/depth within2; Python3.10 grammar and Ruff passed. Qualification ran in a separate checkout. The separately active real-day candidate was preserved.
