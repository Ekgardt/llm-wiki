# Exact synthetic fixture findings in the native secret scan

Date: 2026-11-03. Gitleaks 8.30.1 reproduced the candidate's 19 findings locally
with --all --full-history and redacted output. Its official release archive was
verified against the release SHA-256 checksum before execution.

Twelve findings in queue_evidence_records/fixtures/existing-records.json came from
nine original outputs captured while running 57 existing tests against disposable
queues. The capture plugin is retained locally as /tmp/capture_evidence_original.py;
original results and transfer evidence are in logs/queue-evidence-original-cases.json
and the queue-evidence boundary research. Six findings in queue_task_records fixtures
were captured by /tmp/capture_queue_task_records.py in a TemporaryDirectory under
/dev/shm. The remaining PEM string in lsp_credentials has the literal body abc and
is an input to a redaction test. None of these findings names a provider credential.

Preserve the golden fixture bytes. Add AND allowlists restricted to the exact file,
rule and full original line, including the exact synthetic value. Do not exempt
whole fixture directories, arbitrary hexadecimal strings, all keys, or commits.
The existing scan remains the recurrence gate. Verify changed values and paths
still produce findings with the actual scanner, then scan the complete history.

Source: https://github.com/gitleaks/gitleaks#configuration documents AND conditions,
regexTarget=line and targetRules. This is a narrowly verified false-positive
classification, not evidence that every future fixture value is nonsecret.

Validation: the first exact-line expressions still reported all 19 because the
scanner's Line includes a newline; enabling multiline anchors retains the exact
whole-line match. The final full-history scan covers 2,623 commits / 55.78 MB and
reports zero findings. Nine actual-scanner controls pass: for each of the three
files the original bytes are accepted, a changed synthetic value is detected, and
the original value at another path is detected. No fixture bytes changed.
Evidence: logs/native-ci-gitleaks-before.json, native-ci-gitleaks-after.txt and
native-ci-gitleaks-negative-controls.json. The same scan in fresh native CI remains
pending; the earlier main run uses its older configuration and scans all refs.
