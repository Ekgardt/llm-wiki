# A name loaded is a name used — and a table nobody writes is not an answer

Dated research for two design changes made on 2026-08-29, both of them repairs
to answers that were silently wrong. Rule 2: current practice checked on the
date of the change, before the change.

Both defects were found by the `CODE-07` paired stand and recorded in
`docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md` (commit `2cdd460`) and
`docs/research/2026-08-29-code-parity-rerun-and-the-safety-claim.md`.

---

## 1. `find_dead_code` called live code dead

### What was measured here first

`NEW-135` (2026-08-28) split the dead-code answer into three outcomes:

* a protocol dunder is **not a candidate**, because the language calls it and
  no call site ever writes its name;
* a name some call text mentions is `unresolved_receiver` — doubt;
* a name **nothing** mentions is `zero_confirmed_incoming_calls` — defensible.

The third verdict read its evidence from `EvidenceGraph.call_target_names()`,
which is `SELECT DISTINCT target_text FROM observation WHERE edge_type='CALLS'`.
That is every name a **call site** writes. It is not every name the corpus
writes.

Measured 2026-08-29 against the active generation
(`generation-18d027d063cc5613-711f3ab7`, 29,310 nodes, 71,935 assertions):

| verdict | before | after |
|---|---:|---:|
| `zero_confirmed_incoming_calls` | 461 | **26** |
| `unresolved_receiver` | 412 | 398 |
| `referenced_without_call` | — | 408 |
| not a candidate | 2,895 | 2,968 |

(The audit entry for `2cdd460` records 435 rather than 461 because it measured
a different generation; the table above is one generation,
before and after, so the two numbers are comparable.)

**402 of the 461 names the strongest verdict called dead are named somewhere in
the same corpus** — 87%. The answer refuted itself inside a single run:
`_architecture_dependencies` and `_architecture_symbol` were listed dead while
sitting as values in `_ARCHITECTURE_MODE_QUERIES`, the dict the same run
dispatched through.

The reason no edge exists is structural, not a bug in extraction.
`code_extractor.collect_python_edges` walks `ast.Call` nodes and emits `CALLS`
for the callee; a bare `ast.Name` load — `Thread(target=_worker)`,
`{"run": _worker}`, `set_defaults(handler=_worker)` — is visited by nothing.
Confirmed against the stored graph: the assertion table holds only `CALLS`,
`DEFINES`, `IMPORTS`, `CONTAINS`, `LINKS_TO`, `EXPOSES`, `INHERITS` and two
knowledge edge types. There is **no `USAGE` or `CALL_REFERENCE` edge kind at
all**, and `occurrence.role` is only `definition` or `event`. The graph records
nothing for a value reference.

### What current practice says

[Vulture](https://github.com/jendrikseipp/vulture) — the reference Python
dead-code tool — answers exactly this question, and answers it the way this fix
does. Its documentation: it "uses the `ast` module to build abstract syntax
trees for all given files. While traversing all syntax trees it records the
names of **defined and used objects**." Its own whitelist example shows a bare
reference `Greeter.greet`, never called, is enough to mark the symbol used.
Vulture's confidence table is the same graded shape as `NEW-135`'s verdicts:
100% for an unreachable branch or an unused argument, 90% for an import, and
**60% for a function, method, attribute, class or property** — it never claims
certainty for a function, precisely because a name can be reached dynamically.

Vulture also ships `--ignore-decorators`, because a decorated function may be
held by framework code; and its documentation states plainly that it fails to
see a method invoked through `getattr()`.

Ruff's `F401` has the same documented limits from the other end: side-effect
registration imports are standard Django practice and Ruff cannot see them, and
it "does not understand the globals magic" behind `__getattr__`. The general
statement in both projects is the same — correctness here "would require whole
program reasoning".

For the framework-dispatch class specifically, the `ast.NodeVisitor` pattern is
the textbook example: `visit()` reaches `visit_Call` through
`getattr(self, 'visit_' + node.__class__.__name__)`, so `visit_Call` is called
on every run and named at no call site anywhere.

### The decision

Three verdicts, unchanged in shape; each made true by widening the evidence the
third one rests on, and by moving two more classes into the "not a candidate"
outcome for the same reason dunders are already there.

1. **A new bounded reader, `EvidenceGraph.python_sources()`**, returns the
   stored bytes of every Python source. A new module,
   `scripts/value_references.py`, parses them once and records every name that
   appears in a **load** position: `ast.Name` loads, `ast.Attribute` loads, and
   identifier-shaped string constants. A candidate no call site names but the
   corpus loads is `referenced_without_call` — doubt, not death.

2. **`_dispatched_definition`** removes from candidacy a definition the corpus
   hands over at its definition site: any decorated definition whose decorator
   is not a descriptor (`staticmethod`, `classmethod`, `property`,
   `abstractmethod`, `cached_property`, `override`, `overload`), and any method
   of a class that declares a base. Measured: 53 definitions, of which 14 are
   `visit_*` on `ast.NodeVisitor` subclasses, 2 are `readinto` on
   `io.RawIOBase`, 1 is `redirect_request` on
   `urllib.request.HTTPRedirectHandler`, and 2 are `@pytest.fixture` autouse
   fixtures in `tests/conftest.py`.

3. **The shrink is reported, never silent.** The answer now carries
   `excluded_count`, `excluded_by_rule`
   (`conventionally_reachable: 2840`, `protocol_invoked: 75`,
   `framework_dispatched: 53`) and the coverage the reference index was built
   from (`reference_parsed_sources: 425`, `reference_lexical_sources: 1`).

### What was rejected, with the measurement that rejected it

**A lexical or token scan instead of an AST walk.** Same corpus, same
candidates:

| method | cost | distinct names | survivors of 461 |
|---|---:|---:|---:|
| AST load positions | 3.65 s | 24,579 | **59** |
| `tokenize` NAME tokens | 2.06 s | 30,605 | **0** |
| regex identifiers | 0.41 s | 40,901 | **0** |

A token scan sees every name in its own `def` line, so it can never say
"nothing names it": the third verdict collapses to nothing and the tool quietly
becomes useless while looking cheaper. That is the "shrink into a different lie"
this repair had to avoid, and it is why the extra 3.2 seconds are paid.

**A new edge kind in the extractor.** It would answer the same question, and it
would invalidate every published generation — the failure recorded as `NEW-81`,
where a typing-rule change alone emptied the active pointer and search returned
zero rows until the next nightly build. The stored bytes were already there;
asking them a better question costs one reader and no rebuild.

**String constants: kept, with the cost named.** Reading identifier-shaped
string constants rescues 7 live symbols a load-only sweep still called dead —
`markdown_transaction.py`'s `{"project_lease": "_check_lease_precondition"}`
table (5) and `operational_ownership.py`'s
`{"Windows": "_windows_process_start_identity", "Darwin": …}` table (2), both
resolved through `getattr`. It also moves 4 genuinely unused symbols from the
dead verdict into doubt, because a test fixture spells their names
(`_record_dedupe` ×2, `_search_backends`, `_flush_started`). The trade is
deliberate and asymmetric on purpose: the failure being repaired is calling
live code dead, and a symbol moved to doubt is still in the answer, while a
symbol wrongly called dead is an instruction to delete working code. A
docstring is not an identifier, so prose that merely mentions a name — the
`mark_attempt` case — does not rescue anything.

**Refusing on a source that will not parse.** One deliberately broken fixture
(`tests/fixtures/code_kernel/python/pkg/broken.py`) would then disable the third
verdict for the whole repository forever. Instead an unparsable source falls
back to a lexical identifier scan of that file alone: strictly wider, so it can
only withhold a dead claim, never make one.

### Verified by hand

All 26 survivors were checked with a word-boundary grep across
`scripts/ tests/ benchmark/ integrations/ docs/ skills/ rules/`. Every apparent
hit is a different symbol (`_detect_agent_strengths`, `_git_status_records`,
`lint_memory._is_backlink_exempt`, `doctor._recover_stale_leases`), a prose
mention in a document outside the Python corpus (`assess_claim_contradictions`,
`index_as_of`, `mark_attempt`, `publish_v2_fixture`), or a test whose *name*
contains the substring. None of the 26 is called or referenced.

---

## 2. `mode=dependencies` answered `[]` for everything, always

### What was measured

`EvidenceGraph.dependencies()` walks the `dependency` table. In the active
generation that table holds **0 rows**, as it does in every generation on this
disk, while the same file holds **3,934 `IMPORTS`** and **37,918 `CALLS`**
assertions.

The reason is not corruption. No producer writes a dependency row:
`SourceExtraction.dependencies` defaults to `()`, `code_extractor` never sets
it, `knowledge_extractor` never sets it, and the only writers of
`dependency_id` anywhere in the repository are five test fixtures. The table is
schema with no producer, and the walk over it is a query that cannot return a
row. The only caveat the answer carried was `graph_complete: false`, which is
byte-identical on answers that graded correct — so a caller could not tell an
empty answer from a real one.

The same root has a loud face. `_architecture_dependencies` and
`_architecture_symbol_dependencies` forward `request["symbol"]` — a *name* —
into `find_dependencies(node_id=…)`, and a node id is validated by
`_NODE_ID = [A-Za-z0-9][A-Za-z0-9._:/#@+\-]{0,511}`. Every leading-underscore
name fails the first character class, so `mode=symbol` raised on every private
symbol; and a name that passes the regex matches no node and yields `[]` with
no error.

### What current practice says

A code property graph answers "what does this depend on" by traversing the
edges it holds — Joern's traversal model is node-type steps plus repeat steps
over the CPG's own edge layers, not a separate precomputed dependency relation.
There is no practice anywhere of keeping a dependency answer in a table that
the extractor does not fill. `find_callers` and `find_callees` in this same
module already resolve a *name* through `graph.find_nodes(name=…)`; only
`find_dependencies` did not.

### The decision

* A new bounded reader `EvidenceGraph.reachable(node_id, edge_types=…)` walks
  the assertions that exist. It is breadth-first and expands each node exactly
  once, so its cost is the reachable subgraph rather than the number of paths
  through it. `dependencies()` carries its visited set as a string down every
  path, which is why it needs a work ceiling at all; the first shape tried here
  copied that and took **31.6 s** for the 323-node reverse import closure of
  `scripts/page_status.py`. The per-node form takes **0.24 s** for the same
  323 nodes, and still refuses — not truncates — at both the work and the row
  ceiling.
* `find_dependencies` resolves a symbol name the way callers/callees do, over
  kinds `class, function, method, module`; then a repository-relative **path**,
  because that is what the mode is actually called with
  (`scripts/retrieval.py`, not `scripts.retrieval`); then a literal node id.
  The path form was the reason two stand tasks kept answering nothing after
  the first fix landed.
* The report gained `symbol_resolved`, `resolved_symbol_nodes`,
  `dependency_edge_types` and `dependency_max_depth`, so an empty answer states
  whether the symbol was found at all and how far the walk was allowed to go.
  That is the difference between "this depends on nothing" and "there is no
  such symbol", and it is what made the old empty answer a silent lie rather
  than a wrong one.
* One behaviour difference from the old recursive form, chosen deliberately:
  the seed itself is never listed as its own dependency, even when a cycle
  reaches back to it. That is the whole of the 347-vs-346 difference measured
  on `scripts.retrieval`.

`EvidenceGraph.dependencies()` and the `dependency` table are left in place and
untouched: they have their own tests, and filling the table is a builder change
that this work is not allowed to make. The reader that agents actually use no
longer depends on them.

### Verified

`scripts.retrieval` → 7 direct imports (`evidence_graph`, `graph_neighbors`,
`provenance`, `repository_scope`, `reranker`, `retrieval_telemetry`,
`search_memory`), checked line by line against the `import` statements in
`scripts/retrieval.py`; 347 transitive at depth 8, in 1.0–1.3 s.
`scripts.page_status` → 0, and correct: that module imports only
`__future__`. `_page_status` → `_extract_frontmatter_field`, where it used to
raise. `page_status` and `_fuse_rrf` → `symbol_resolved: false`, because no node
carries those names.

---

## 3. What the paired stand said, before and after

`benchmark/run_code_parity.py`, both llm-wiki columns, run on the same active
generation before and after — so the generation is not a variable.

| | before | after |
|---|---:|---:|
| `llm_wiki` correct /13 | 8 | **10** |
| `llm_wiki` partial | 0 | 1 |
| `llm_wiki` wrong | 5 | **2** |
| tool errors | 1 | **0** |
| wrong-but-confident | 4 | **2** |
| operator-attention events | 1 | **0** |
| tokens | 87,530 | 137,775 |
| wall seconds | 89.1 | 122.8 |
| `llm_wiki_best` correct /13 | 11 | 11 |

Three grades moved and none moved down: `T04` (`mode=symbol` on
`_page_diverse`) wrong-by-crash → partial, `T08` (reverse imports of
`scripts/page_status.py`) wrong → correct, `T09` (imports of
`scripts/retrieval.py`) wrong → correct.

**The token cost is real and is named.** All of it is `T08` and `T09` going
from 107 tokens each — the price of answering nothing — to 24,944 and 24,508,
which is the 25,000-token client budget being filled by a depth-8 transitive
closure. The rows are depth-sorted, so the budget cut takes the deepest first
and the direct dependencies the caller asked for always survive; but a caller
who wants only those is paying for eight levels. The default depth is where a
thrift decision belongs and it was deliberately not made here: `mode=symbol`,
`mode=callers` and `mode=callees` already answer the one-hop question, and
narrowing `dependencies` to match them is a semantic change, not a bug fix.
`dependency_max_depth` is now in the envelope so the bound is at least visible.

**One regression was caused and then removed.** The first pass appended
`referenced_without_call` to the end of `DEAD_CODE_REASON_ORDER`, which is the
order the answer budget cuts from. `T07` (`_search_backends`) went correct →
wrong because its row was cut, while rows stating weaker evidence survived. The
order now runs by strength of the deadness claim — nothing names it, then named
but never called, then a call site names it — which is what the constant's own
comment already said the rule was.

---

## Sources

- [vulture — Find dead Python code](https://github.com/jendrikseipp/vulture)
  (detection by defined/used names; confidence table; `--ignore-decorators`;
  the documented `getattr` miss)
- [vulture on PyPI](https://pypi.org/project/vulture/) (`--min-confidence`,
  whitelist workflow)
- [Ruff `F401` unused-import](https://docs.astral.sh/ruff/rules/unused-import/)
  and [astral-sh/ruff#18893](https://github.com/astral-sh/ruff/issues/18893)
  (side-effect registration imports, `__getattr__`, the whole-program-reasoning
  limit)
- [Why Your Python Dead Code Detector Is Wrong About FastAPI, SQLAlchemy, and
  Half Your Codebase](https://dev.to/orenlab/why-your-python-dead-code-detector-is-wrong-about-fastapi-sqlalchemy-and-half-your-codebase-2gc4)
  (framework dispatch as the dominant false-positive class)
- [Joern — Code Property Graph](https://docs.joern.io/code-property-graph/) and
  [Traversal Basics](https://docs.joern.io/traversal-basics/) (dependency
  questions answered by traversing the graph's own edges)
- [`ast.NodeVisitor`](https://docs.python.org/3/library/ast.html#ast.NodeVisitor)
  (`visit()` dispatches by computed method name)
