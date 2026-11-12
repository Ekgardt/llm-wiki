# Python value-reference analysis

`scripts/value_references.py` is qualified in place after the separate spelling
repair02a08f75. This ownership change preserves every production byte. The reader
accepts supplied `(path, bytes)` sources; it does not open repository files, query
a generation, issue a dead-code verdict or mutate an index. Its only product
import is the complete `python_parse` implementation included in this packet.

The index retains value names, exact path/name/line dispatch evidence and source
coverage counts. Scoped indexes refuse queries for names outside their declared
scope. Malformed sources retain the existing lexical fallback; decorated
functions and methods dispatched by a base retain their conservative treatment.
The AST decides whether a name is a reference. Source admission uses a raw-hit
fast path and standard Python spelling rules, including normalization, decoded
literals and concatenation. A conservative token hit alone proves no reference.
The runtime remains the approved Python3.14 series; no new package or cache.

Six original pure tests move unchanged from the mixed stored-graph test file.
The complete scoped-reader test file stays at its original path and belongs to
this owner. Ten stored-graph/MCP scenarios and the cross-language/verdict policy
checks remain integration responsibilities, with their assertions intact. Their
remaining ownership is visible in the whole-project inventory; this packet does
not claim that every consumer integration has been fully decomposed.

## Research and limits — 2026-11-04

The complete historical research and timing reports accompany the source, as
does the spelling-repair research. Historical benchmark results remain dated
history. The old claim that byte-substring absence proves semantic absence is
superseded by the actual escaped-string/Unicode regressions; it is not current
contract. The historical rejection of token scans as reference evidence still
applies: tokens only decide admission, and the existing AST decides the result.

The [Python lexical rules](https://docs.python.org/3.14/reference/lexical_analysis.html),
[Unicode identifier rules](https://www.unicode.org/reports/tr31/) and
[Vulture's documented static-analysis limits](https://github.com/jendrikseipp/vulture)
were checked for this boundary. Keep one coherent reader with its existing
parser dependency. Per-helper modules, a new code graph, another parser package
and a cache with unqualified invalidation would add responsibility without
improving this ownership boundary.

The repaired prefilter is materially slower than the incorrect byte-only shortcut:
three-run268-source medians1.491s versus0.088s for the measured symbol query,
compared with2.939s for full analysis. No unchanged-latency or full-cycle LLM
savings claim. See the complete repair report for the absent-symbol query and
rejected prototypes. No arbitrary input cap is added to hide this cost.

Qualification: unchanged production hash and original test ASTs, isolated import
allowing only python_parse, actual complexity/shape, measured test weights,
complete dependency guides, whole authority files and a byte-exact packet within
100000o200k_base tokens. This is neither deployment nor whole-project completion.
