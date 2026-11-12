# Lint owns source bytes and rechecks them

Research and qualification date: 2026-10-13.

The normal lint command exceeded the nightly's existing 120-second budget. A
separate instrumented observation found 13,040 evidence resolutions and 102,839
SHA calls, including 75.516 seconds in SHA itself. It also measured 49.084 seconds
in repeated directory discovery for links. The diagnostic alarm was caught by
a check and the observation continued to 187.292 seconds; this is explicitly not
a successful bounded maintenance run. A late stack attachment found the process
already gone and establishes no cause.

Four references to one daily source reproduced four independent physical reads
through the public lint entry point. Claim lint reproduced the same behavior
across pages carrying complete 17,000-character quotes and decomposed Unicode.
Ordinary independent evidence reads need fresh source checks. One read-only lint
operation can instead explicitly own its source bytes, validate every reference
against them, and re-read every owned source before its result is returned.

## Research and choice

- [Python context managers](https://docs.python.org/3/library/contextlib.html#contextlib.contextmanager)
  support a scope whose normal exit completes validation before a caller's
  return escapes. The implementation uses the Python 3.10-compatible API.
  [Context-variable guidance](https://docs.python.org/3/library/contextvars.html)
  was also reviewed for operation isolation; an explicit owner object avoids
  adding ambient shared state here.
- [MITRE CWE-367](https://cwe.mitre.org/data/definitions/367.html) explains the
  danger of checking an object and then using a changed object. Stat-only reuse
  is rejected: current contents are read and hashed again at scope completion.
- [SQLite isolation](https://sqlite.org/isolation.html) distinguishes a bounded
  snapshot view from repeated current reads. Markdown has no database MVCC;
  this implementation makes no filesystem snapshot or OS sandbox promise.

Raising the lint timeout leaves repeated full-source work. Removing source or
quote checks weakens the evidence contract. Reusing file metadata cannot detect
a same-size edit with restored timestamps. A persistent source cache would need
invalidation and an architectural decision. The selected scope is operation
local and owns exact immutable byte objects through the existing byte resolver.

`EvidenceResolver.resolve()` still reads and checks the current file on every
independent call. The explicit `captured_sources()` scope returns provisional
results that must not be published or acted on before normal exit. Its two
consumers are read-only lint checks: their findings do not escape before exit.
Every captured flat source is freshly read and its full digest compared before
return. Disappearance, unreadability, or changed content refuses the operation.
Archive references keep their existing independent full verification. Historical
parts retain their source hash, block, span, UTF-8, line, and full-quote checks.

The captured view closes on both success and exceptions, releases its owned
byte-proof buffers, and refuses use after exit. Its byte resolver is separate
from the original resolver, so repeated scopes do not retain old captured source
bytes in the original object's immutable-proof map.

No new deadline, byte cap, setting, runtime path, or database is introduced.
Memory use is proportional to the sources held by this operation. This is an
explicit source view with final verification; it does not record every transient
external edit or make the whole knowledge tree atomic.

## Qualification boundary

Regression checks cover repeated ordinary and claim reads, full result equality,
source disappearance, same-size modification with restored timestamps, fresh
independent operations, and refusal before claim findings are returned. The
original public lint implementation fails the repeated-read regression.

Real live-vault and final installed maintenance results are qualified separately.
The other measured directory-discovery cost, historical losses, native hook
coverage, and the wider numerical-limit audit remain separate obligations.

The initial live candidate finished the normal lint command in 52.254 seconds
under the unchanged 120-second budget: one current-day orphan and four owed
backlinks, no evidence or claim findings. This is an uncommitted candidate result,
not a matched whole-cycle benchmark or a final installation claim. The canonical
backlink repair then applied all four with no failures in 36.954 seconds.

The preceding full CI also observed an unrelated Windows health-start test
taking 6.63 seconds against its unchanged 0.5-second assertion. Linux's single
instrumented reproduction passed at 0.127 seconds; the Windows cause is not
established. That test now emits a stack at its existing failure threshold and
includes the actual elapsed time, without altering its bound or secret checks.
This is diagnostic evidence collection, not a claimed production repair.
