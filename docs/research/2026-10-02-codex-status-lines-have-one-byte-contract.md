# Codex installer status lines have one byte contract

Research date: 2026-10-02. Windows CI across Python 3.10–3.13 reports the Bash installer rewrite check returning 1 with the earlier entry still stale. The machine-readable native Python status carries CRLF; Bash command substitution removes LF but retains CR, so the shell never matches `stale` and does not request replacement. Two pre-fix regressions reproduce CRLF using the actual Python TextIOWrapper on this Linux host.

The two config status commands now emit UTF-8 bytes with LF through the binary stdout buffer. Human-facing output, entry classification, verified preimages, foreign-entry consent and installer error propagation remain unchanged. This gives the existing one-line machine protocol a platform-independent ending; it adds no path, setting or dependency.

Alternatives: removing all carriage returns in the shell is broader than the status protocol and duplicates consumer handling; changing tests to normalize output would conceal the producer difference. Emit the exact protocol at its producer. Actual Windows qualification still requires the subsequent CI run; Linux regression success is not Windows execution proof.

Independent primary sources checked: [Python text newline translation and binary I/O](https://docs.python.org/3/library/io.html), [GNU Bash command substitution](https://www.gnu.org/s/bash/manual/html_node/Command-Substitution.html), [Microsoft text and binary streams](https://learn.microsoft.com/en-us/cpp/c-runtime-library/text-and-binary-streams?view=msvc-170). Direct GNU fetch timed out but its official indexed manual supplied the relevant semantics. The shared invariant is that machine protocol bytes must not depend on native text translation.

The real configuration helpers are exercised, including preserving other settings and verified previous configuration. CLI nonzero errors remain errors. No historical capture failure is cleared by this repair.
