# The tokenizer vocabulary is pinned third-party data, not a module

Date: 2026-11-11. tiktoken 0.14.0, Python 3.14.

## Problem

Module budgets are measured with tiktoken's `o200k_base` encoding. tiktoken
downloads that vocabulary from `openaipublic.blob.core.windows.net` on first use;
the checker and offline CI runners cannot reach that host, so the suite vendors the
file at `tests/data/tiktoken/fb374d419588a4632f3f557e76b4b70aebbca790` and points
`TIKTOKEN_CACHE_DIR` at that directory while it measures. The ownership gate then
reported the file as tracked but owned by no module.

## Facts

- tiktoken looks a cached vocabulary up under `TIKTOKEN_CACHE_DIR` by the SHA-1 of
  its URL, and accepts the cached bytes only when their SHA-256 equals the
  `expected_hash` its encoding constructor names; otherwise it removes the file and
  downloads again (installed `tiktoken/load.py::read_file_cached`,
  `tiktoken_ext/openai_public.py::o200k_base`).
- The vendored file is 3,613,922 bytes and 2,209,602 `o200k_base` tokens, 22 times
  the 100,000-token module limit. No module can own it whole, and record ownership
  measures the selected bytes the same way.
- Community offline guides describe the same route: prime the cache on a connected
  machine and copy it with its hash-derived file names unchanged.

## Alternatives considered

1. Download during CI. Refused: the platform checker is offline, and it would add a
   network host to every test run.
2. A wheel that bundles the vocabulary (for example `quicktok-v1`). Refused: it is
   a different tokenizer implementation, so the measured counts would no longer be
   tiktoken's, and it adds an unreviewed dependency to the measuring tool.
3. Module ownership of the file. Impossible: 22 times the limit.
4. An exemption by directory or by file class. Refused: the ownership contract
   forbids an unbounded bucket, and any file added there would pass unseen.
5. **Chosen:** a per-file pin. `tests/module_ownership.py::PINNED_VOCABULARIES`
   names each vendored vocabulary by its source URL and SHA-256. The file is
   covered only if its name is the SHA-1 of that URL, its bytes match the digest,
   no other tracked file sits in the directory, and no module also claims it. Only
   tracked files are inventory, so a git-ignored `.DS_Store` or an interrupted
   download beside it does not fail the gate. The report counts it in
   `pinned_count`, never in `owned_count`. The pin is
   tested to equal the `expected_hash` that the installed tiktoken verifies, so a
   tiktoken upgrade that changes the vocabulary fails the suite instead of silently
   measuring with a stale file.

## Trade-offs

The vocabulary is reviewed as data by its digest, not read by a maintainer. A
corrupted copy is refused by both the ownership gate and tiktoken; tiktoken then
deletes it and, offline, measurement fails loudly rather than counting with wrong
data. Removal condition: when tiktoken ships its vocabularies inside its wheel, or
the measurement moves to a tokenizer that does, delete the vendored file and its
pin together.

Sources:
- tiktoken source, `tiktoken/load.py` and `tiktoken_ext/openai_public.py`
  (installed 0.14.0; upstream https://github.com/openai/tiktoken).
- https://markaicode.com/errors/tiktoken-connection-refused-fix/ (offline cache
  priming and SHA-1 file names).
- https://community.openai.com/t/newconnectionerror-keeps-coming-up-over-a-tiktoken-file/670052
  (the failure an offline host meets).
- https://pypi.org/project/quicktok-v1/ (bundled-vocabulary alternative).
