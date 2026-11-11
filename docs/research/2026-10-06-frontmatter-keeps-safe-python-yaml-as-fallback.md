# Safe YAML acceleration keeps the original parser as fallback

Research and qualification date: 2026-10-06. Installed PyYAML is 6.0.3 with LibYAML. This change accelerates only the common frontmatter parser. It does not qualify a full MEMORY generation or explain its measured 255-second snapshot capture.

The local `SafeLoader` and `CSafeLoader` use the same `SafeConstructor` and `Resolver`. The optional C parser is used only when present. On a YAML error it retries the original `safe_load`, preserving the existing error-class problem text. Missing C support uses the original parser. Unexpected failures remain visible. UTF-8 validation, mapping/nonmapping outcomes, metadata types, boundaries and caller deadlines remain unchanged. No unsafe loader, new dependency, configuration, cache or source authority is introduced.

Primary sources checked on the research date:

- [PyYAML documentation](https://pyyaml.org/wiki/PyYAMLDocumentation) explicitly warns of subtle Python/C differences. Its old installation examples and unsafe `CLoader` example are not adopted.
- [YAML 1.1 specification](https://yaml.org/spec/1.1/) describes the data semantics, not an implementation-equivalence guarantee.
- [PEP 399](https://peps.python.org/pep-0399/) motivates accelerator/fallback compatibility checks for the standard library; it does not guarantee third-party PyYAML compatibility.
- [Upstream PyYAML 6.0.3 release](https://github.com/yaml/pyyaml/releases/tag/6.0.3) matches the installed version.

Alternatives were retaining pure Python, requiring the C extension, caching frontmatter, and replacing YAML. Optional safe acceleration is smaller and preserves installations without C. A cache would add identity/retention questions; replacing the parser would introduce unnecessary semantic risk.

Read-only comparison of all 1,772 current note headers and 1,998 receipt headers (979,355 header bytes) found equal typed results, body offsets and exact problem strings. Sixteen malformed/unsafe/type/alias controls also matched. An isolated candidate then alternated Python/C/Python/C on one captured immutable set through the actual common parser: 1.358/0.179/1.347/0.174 seconds, with identical outcomes and unchanged physical sources. This is one shared-host header-only experiment while another model experiment ran; it proves neither whole-generation speed nor every platform/input equivalence.

The original common path is correct but slower in the measured scenario. There is no claimed functional RED for choosing Python. The initial new-control run had 3 failures and 15 passes: one test-harness mistake assumed a keyword where the original loader uses a positional argument; two controls require newly introduced C failure handling and are not original functional defects. That log is retained. Final controls cover absence of C, exact Python classification after C error, unexpected exception propagation, unsafe tags, UTF-8, dates/booleans/merges, source boundaries and expiry. Existing tests and their assertions are unchanged.

Qualification: 389 related tests passed, six skipped. All nine changed/new/nested callables meet CCN <= 5, at most two `if` statements and nesting <= 2; Ruff and Python 3.10 grammar passed. A real full-cycle generation remains required before claiming product-wide benefit. The Python fallback stays necessary for portable installations and original diagnostics.
