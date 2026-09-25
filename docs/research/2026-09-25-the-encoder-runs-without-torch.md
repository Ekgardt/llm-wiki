# The encoder runs without torch

Date: 2026-09-25. Owner's request the same day: make the command-line search stop
paying for the library load (audit item C-11), and clean the legacy afterwards.

## Question

A command-line search spends about 6.5 s loading the embedding model and a few
hundredths of a second using it. Can the query path load the same model without
`torch` and `sentence-transformers`, and give the same vectors?

## Sources

- Hugging Face, `intfloat/multilingual-e5-small`, file listing and `paths-info` at
  the pinned revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` (fetched
  2026-09-25): the repository ships `onnx/model.onnx` (470 268 510 bytes, LFS
  SHA-256 `ca456c06…8665`), and `onnx/tokenizer.json` is the same blob as the root
  `tokenizer.json` (`0b44a9d7…4c39`). The model's `modules.json` is Transformer →
  Pooling (mean tokens) → Normalize; `sentence_bert_config.json` sets
  `max_seq_length` 512.
- Sentence Transformers, "Speeding up Inference" (fetched 2026-09-25,
  https://sbert.net/docs/sentence_transformer/usage/efficiency.html): the library's
  own `backend="onnx"` uses a repository's `onnx/model.onnx`, but "the ONNX backend
  still requires PyTorch".
- ONNX Runtime Python API (fetched 2026-09-25,
  https://onnxruntime.ai/docs/api/python/api_summary.html):
  `InferenceSession(path, sess_options=..., providers=[...])`, `run(output_names,
  input_feed)`; `intra_op_num_threads` 0 lets the runtime choose.

## Measurements (facts, this machine, warm disk cache)

| Path | Import and load | First encode |
|---|---|---|
| `sentence-transformers` 5.6.0 on `torch` 2.13.0 (today) | 6.46–6.52 s | 0.02–0.69 s |
| `onnxruntime` 1.27.0 + `tokenizers`, mean pooling, L2 norm | 1.48–1.86 s | 0.01 s |

- `import sentence_transformers` alone: 4.79 s; `import torch`: 1.34 s;
  `import onnxruntime`: 0.20 s; `import tokenizers`: 0.02 s.
- The two paths give the same vectors: maximum absolute difference 1.2e-07,
  cosine 1.0000000 on a Russian query and an English passage. The vectors in the
  active generation therefore stay valid; nothing is rebuilt.
- On 402 texts from the vault (400 passages from 120 pages, two queries): maximum
  absolute difference 1.42e-07, minimum cosine 0.999999821, the same top five
  passages for both queries.
- Batch encoding of those 402 texts: `torch` 5.86 s. ONNX with batches padded to
  their longest text by characters: batch 8 5.92 s, 16 6.82 s, 32 8.48 s, 64
  12.18 s. Sorted by token count and padded per batch: batch 4 4.84 s, 8 5.31 s,
  16 5.43 s, 32 7.50 s. The encoder uses token-count order and batch 4.
- A cold command-line search on the live vault, end to end: 8.18 s with `torch`,
  2.26–2.30 s with ONNX. The five results of three questions (Russian and
  English) were identical.

## Findings

1. The only product users of the model are `search_memory` (queries, and the
   generation builder's passages) and `query_memory`'s turn pruner, both through
   `embedder.encode(prefixed_texts(...))`. The retrieval benchmark measures the
   native library on purpose and keeps its own extra.
2. The reranker loads with `torch` and `transformers`; `optimum` is declared in the
   `reranker` extra and imported nowhere, and its comment says "via ONNX", which
   the code does not do.
3. `install_models.py` fetches `model.safetensors` for the encoder. After this
   change nothing reads that 470 MB file.
4. `_embedder_failure_kind` recognises a missing model partly by the substring
   `NotFound` in an exception's class name.
5. Two doctor tests of a healthy vault (`test_report_schema_and_all_check_classes_are_json_safe`,
   `test_cli_returns_zero_for_healthy_report`) read this machine's real Hugging
   Face cache: `huggingface_hub.try_to_load_from_cache` takes
   `constants.HF_HUB_CACHE` at call time. With the semantic extra installed they
   passed while the cache held the old weights and failed once the encoder
   needed `onnx/model.onnx`, which this machine has not fetched yet.

## Decision (conclusion)

- A new module, `scripts/onnx_encoder.py`, loads `onnx/model.onnx` and
  `tokenizer.json` from the local Hugging Face cache at the pinned revision (never
  the network) and encodes with mean pooling over the attention mask and L2
  normalisation, sorted by token count in batches of 4 padded per batch,
  truncating at 512 tokens. A file
  absent from the cache raises `FileNotFoundError`, so a missing model is named by
  its type alone.
- `search_memory` and `query_memory` use it; the `semantic` and `hybrid` extras
  depend on `onnxruntime`, `tokenizers`, `huggingface_hub` and `numpy` instead of
  `sentence-transformers`, so an install without the reranker no longer pulls
  `torch`.
- `install_models.py` pins and verifies `onnx/model.onnx` for the encoder, keeps
  `model.safetensors` for the reranker, and removes the encoder's retired
  `model.safetensors` from the cache once the ONNX weights are verified.
- The healthy-vault doctor tests build the pinned files in a Hub cache of
  their own, as an install leaves them, like the rest of the state they build.
- Legacy cleanup: `optimum` leaves the `reranker` extra and its comment is
  corrected; the `NotFound` substring rule goes; CI's hybrid job checks the new
  imports.
- Rejected: `SentenceTransformer(..., backend="onnx")`, because it still imports
  `torch` and `sentence-transformers` (4.79 s of the 6.5 s); a quantised
  `model_qint8_avx512_vnni.onnx`, because it changes the vectors and would need a
  rebuild and a quality measurement.

## Edited files

- `scripts/onnx_encoder.py` (new), `scripts/search_memory.py`,
  `scripts/query_memory.py`, `scripts/embedding_model.py`,
  `scripts/install_models.py`, `pyproject.toml`, `uv.lock`,
  `.github/workflows/tests.yml`
- tests of the encoder, the installer, the embedder failure reasons and
  `tests/test_doctor.py`
- `README.md`, `README.ru.md`, `README.zh-CN.md`, `docs/ARCHITECTURE.md`,
  `docs/USER-GUIDE.md`, `CHANGELOG.md`, `tests/test_readme_i18n.py`

## Uncertainty

On the first night after the update, the compile's own generation refresh runs
before the `models` step has fetched the ONNX weights. If pages changed, that
refresh publishes a generation without vectors; the nightly's later refresh,
which runs after the `models` step, restores them in the same pass.

The 1.5 s left is mostly the runtime building the 470 MB session. The load was
measured on this machine only; Windows and macOS CI run the tests, not the
timing.
