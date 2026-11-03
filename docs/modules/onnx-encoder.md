# Local ONNX encoder

Owns `scripts/onnx_encoder.py` in place. The source is unchanged by qualification.
It resolves model/tokenizer files in the local cache at the caller's explicit
revision, configures the existing tokenizer truncation, opens the CPU session,
pads batches, mean-pools over the attention mask and normalizes vectors. Output
rows return in input order. Missing cached files raise FileNotFoundError.

The numerical and model limits retain their documented original reasons. No model
revision, inference backend, network policy, dependency or algorithm changes.
`search_memory` keeps model selection, caching of the encoder, diagnostics and
fallback to other search signals. Installers still own fetching weights.

Whole packet: `onnx-encoder-context.json`. It contains the full implementation,
four unchanged original unit tests and fixtures, import guards, instructions and
full historical measurement. Optional numerical/runtime dependencies stay lazy;
the import guard rejects any product dependency. Saved production hashes and
original test ASTs prove the in-place qualification preserves them.

Two whole-product dependency checks remain in
`tests/test_the_encoder_runs_without_torch.py`. Missing-model diagnostics and
local-only query tests remain separate real integration obligations, run with the
module suite; no unchecked contract replaces their implementation dependencies.
The September25 measurements are dated history, not a fresh quality/performance
claim. Existing fake-session unit tests do not independently qualify model quality.

See [research](../research/2026-11-03-onnx-encoder-owner.md).
