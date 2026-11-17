# Offline refusal calibration

`benchmark/calibrate_refusal_gates.py` owns offline twin readings, scalar threshold
selection, date-distance diagnostics and the existing CLI report. Paths, flags,
input/output keys, tune/decide split and algorithm constants stay in place.
The full packet includes evidence_sufficiency, longmemeval_data and longmemeval_score
implementations and their complete guides, bootstrap, instructions and historical
research. No provider call, dataset download or installed vault modification is
needed to calibrate already-staged records. `--json` writes the explicitly named
report destination; normal stdout rendering remains unchanged.

## Behavior and limits

Evidence-labelled turns appear in the original. Twins remove entire evidence
sessions; distractors are the current lexical-overlap proxy, not proof of the
product's actual retrieval window. Silence-labelled questions retain only a twin.
The score threshold is selected from tune records only and applied to both halves.
Scores compare inclusively, AUC ties count half, and existing normal-quantile
clipping and rounding remain unchanged. The separately calculated per-half date
tolerances are diagnostics, not held-out evaluation at a date gate learned only
on tune. Do not describe those diagnostics as unbiased deployed performance.
Empty measured-score populations retain the existing statistics failure; no fake
metrics or successful empty report is introduced. Existing depth/date candidates,
clipping constants and their historical limitations are unchanged.

A reproduced reporting bug is corrected separately from ownership: a missing
threshold means no gate fires. Previously `_half_report` converted `None` to `0.0` and
could report 100 percent firing, contradicting `_fires` and refusal/silence output.
The share helper now accepts None as no firing; numeric `0.0` remains a real threshold.
Other selection, measurement and rendering definitions stay unchanged.

## Research — 2026-11-04

Checked [Python statistics](https://docs.python.org/3.14/library/statistics.html#statistics.NormalDist),
[scikit-learn threshold tuning](https://scikit-learn.org/stable/modules/classification_threshold.html),
and [original LongMemEval](https://github.com/xiaowu0162/LongMemEval). These support
explicit score-versus-decision semantics, separate tuning/evaluation and the
dataset's evidence labels; they do not prove real-world accuracy of this proxy.
Keep one existing implementation rather than add a calibration dependency or a
parallel metric engine. No third-party package or benchmark threshold changes.

Qualification uses red/green no-threshold regressions, independent numerical
examples, tune/decide noninterference, real temporary staged JSON/CLI execution,
isolated imports, actual complexity, measured weights and exact reconstruction
within 100,000 tokens. The complete 2026-09-22 report remains dated historical evidence.
No whole-project completion or full-cycle token saving is claimed.

Dependency implementations and full guides remain here. Their unrelated scoring
research, independent provider test suites and qualification manifests stay in
the provider packets; the consumer includes the complete September 22 calibration
history and the scorer’s relevant refusal-calibration records.
