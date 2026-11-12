# Repeated compile sizing and refreshed capacity

Measured and researched on 2026-10-08. This change fixes two reproduced causes;
it does not qualify the whole product or close audit point 7.

## Original observations

The retained full-run profile recorded about 132 minutes of profiled time.
Thirty-three context refreshes consumed about 80 minutes cumulatively; source
choice construction performed 11,273,210 binding attempts. The run terminated
with `compile budget changed while refreshing context`.

Each prospective optional context page rebuilt the prompt and recomputed the
same original daily-line bindings. Those bindings are pure calculations over
the selected immutable daily snapshots. They are not authority to publish.
Protection of the complete changed prompt and protected aliases must remain fresh.

The measured capacity helper could shrink a previously declared budget even
when mandatory input already fitted it. A separate causal regression reproduced
the exact run exception: adding text to an existing physically required page
changed its measured minimum under the same qualified provider, and refresh
rejected that legitimate recalculation as identity drift.

## Change and boundaries

One sizing measure retains successful original-line calculations, keyed by the
identities of its retained selected immutable parts and the exact date,
timestamp, and quote. Every layout still checks source membership and member
hashes before lookup. Changed selected parts get a different key. Protected
aliases bind afresh, complete prompt protection is repeated, scopes reset on
exception, and final model-answer binding runs outside this sizing cache.
Nothing is stored on disk and no process-global unbounded cache is introduced.

Declared capacity is never shrunk to a smaller measured minimum. A refreshed
budget must exactly equal the normal calculation from the fresh measured input,
previous budget, required context, and unchanged qualified provider. Existing
tests still reject arbitrary budget edits, model changes, configuration failures,
missing required context, and over-capacity complete units. The reservation and
safety margin remain checked. No new limit, schema, path, provider, or runtime
contract is added.

Alternatives: caching all layouts would need policy and full-context invalidation
and retain many large prompts; caching protected bindings would risk stale
redaction; removing budget identity checks would hide arbitrary changes; freezing
the old measured minimum would reject legitimate required context growth.
The chosen scope preserves complete fresh protection and verified provider bounds.

## Evidence and practical limits

The original regressions failed before the fix. Related CPython 3.10 tests:
749 passed. The one initial broad-suite failure was a pre-existing test expecting
draft program v15 while HEAD already supplied v16; its exact expectation was
updated to the actual version. No assertion was removed. Changed production,
test, and diagnostic functions were checked individually for CCN <= 5, at most
two if statements, and control nesting <= 2. Ruff and staged secret scanning passed.

The small 100-row comparison improved from 0.270 to 0.159 seconds; the 750-row
comparison did not demonstrate a speedup (0.900 versus 0.940 seconds). Neither
is presented as full-cycle evidence.

The real comparison used one complete currently pending work unit selected by
the normal receipt predicate and native-unit boundary, with the same frozen
complete context in both arms and no model calls or publication. Refresh plus
final layout took 37.139 seconds uncached and 31.467 seconds with reuse. Binding
calls fell from 110,940 to 180. Selected sources, batch data, prompt SHA-256
764bb2b150c76ed3c34b9c0075279aec56d522508372fde1bb327479b40d2e8f, and schema
SHA-256 0e67b15e7500cf3be13a22c39e72e67f7a5e4f848b151b5349ddf9d8c0b06550 matched
exactly. This is about 15% faster for this refresh, not a full-compiler guarantee.

Two diagnostic attempts failed before measurement because the helper serialized
mapping proxies and a derived partition-owner weak reference incorrectly.
Their stderr is retained. The third uses the previously verified immutable
mapping serializer and omits only the derived weak-reference partition cache
from serialization. Complete source bytes, metadata, hashes and provider
mappings survive an asserted round trip. Actual measurement uses the original
captured inputs. Full useful compilation and full-cycle token cost remain open.

Evidence: `logs/step7-real-refresh-pair-v3-20261008.json`, its immutable capture,
the first two diagnostic stderr logs, and
`/dev/shm/step7-packing-related-final-20261008.xml`.

Primary sources inspected 2026-10-08:

- CPython on memoization, purity and retained arguments:
  https://docs.python.org/3/library/functools.html
- Salsa on derived-query reuse and verification of changed inputs:
  https://salsa-rs.github.io/salsa/plumbing/fetch.html
- Bazel on declared inputs and invalid results when inputs change during work:
  https://bazel.build/remote/caching

These inform the reuse boundary; no Salsa or Bazel dependency is introduced.
