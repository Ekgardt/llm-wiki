---
type: debugging
title: "Case-Sensitive Grep on Injected Context Gives False Failure"
description: "Grepping the injected `additionalContext` payload with a lowercase pattern silently misses content that was written with an initial capital, producing a false 'notes lost' verdict even when the hook ran correctly."
timestamp: 2026-07-03T05:41:37
confidence: medium
source_authority: user
---
# Case-Sensitive Grep on Injected Context Gives False Failure

One-sentence summary: Grepping the injected `additionalContext` payload with a lowercase pattern silently misses content that was written with an initial capital, producing a false "notes lost" verdict even when the hook ran correctly.

## Symptom / Cause / Resolution

**Symptom:** B-sim test (or any verification script) reports `❌ Notes lost` / "context not injected" immediately after confirming that the state.md was created with the correct content.

**Cause:** The injected context is pulled verbatim from `state.md`. Section headers such as `## Where we left off` start with a capital `W`, and sentences start with capitals. A grep pattern like `verified` (lowercase) will not match `Verified` on the first word of a line. The discrepancy produces a false failure that looks identical to a genuine injection bug.

**Concrete example from soak test:**
```bash
echo "$OUTPUT" | grep "verified"   # ❌ — misses "Verified: hook wrote…"
echo "$OUTPUT" | grep -i "verified" # ✓
```

**Resolution:**
1. Always use `grep -i` when searching injected context for keywords that may appear at the start of a sentence or heading.
2. Alternatively, grep for a unique mid-sentence substring (e.g., a project root path) that is guaranteed to be in the same case as the source file.
3. Before concluding injection failed, check whether the state.md itself was created correctly — a successful write + failed grep means the bug is in the test, not the hook.

## Evidence
- `knowledge/daily/2026-04-19.md` [17:09:56] — B-sim false `❌ Notes lost` incident; root cause identified as case mismatch
- `knowledge/daily/2026-04-19.md` [17:14:57] — confirmed same gotcha; "❌ Notes lost" while `'Where we left off' preserved` was passing on identical content

## Related
- [[knowledge/notes/b-sim-hook-testing]] — the testing pattern where this gotcha most commonly appears
- [[knowledge/notes/hook-errors-silent-without-state-root]] — sibling hook-testing debugging entry
