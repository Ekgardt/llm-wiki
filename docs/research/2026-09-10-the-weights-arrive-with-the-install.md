# The weights arrive with the install — 2026-09-10

**Gap.** Nothing in the product downloads a model. `search_memory._get_embedder`
and `reranker._loaded_bundle` both load with `local_files_only=True` (the
first on purpose, so a cold search never opens a connection before reading a
local byte); `sync_memory` "does not install semantic, reranker, code-graph,
or model dependencies"; the guide says `uv sync --extra semantic` "installs
`multilingual-e5-small`", which it does not — it installs the library. On
this machine the two models are in `~/.cache/huggingface/hub` because the
benchmark's `prefetch_models` put them there. A fresh install answers
lexical-only, with `model_unavailable` and `reranker_unavailable` in the
trace, until someone downloads 2.7 GB by hand. That contradicts «всё должно
работать автоматически без привлечения пользователя».

**What is needed, by size.** `intfloat/multilingual-e5-small` at
`614241f6`: `model.safetensors` 471 MB plus tokenizer (22 MB).
`BAAI/bge-reranker-v2-m3` at `953dc6f6`: `model.safetensors` 2.27 GB plus
tokenizer (22 MB). Both repositories also carry files the product never
reads (README, ONNX or `.bin` duplicates elsewhere in the tree, pull-request
refs); a greedy `snapshot_download` pulls them.

**World practice, 2026-09-10.**
- `huggingface_hub.snapshot_download(repo_id, revision=<40-hex>,
  allow_patterns=[...])` is the documented way: a commit hash pins every file
  to one commit, `allow_patterns` keeps the download to the files actually
  read, and the cache records the pin under `refs/` so later runs with
  `HF_HUB_OFFLINE=1` / `local_files_only=True` resolve it without the network
  (https://huggingface.co/docs/huggingface_hub/guides/download,
  https://huggingface.co/docs/huggingface_hub/en/package_reference/file_download).
- The cache is content-addressed: an LFS blob is stored under its SHA-256,
  a small file under its git SHA-1
  (https://deepwiki.com/huggingface/huggingface_hub/2.4-cache-architecture).
  That is not a verification: with the newer XET storage,
  `snapshot_download` has completed with a blob that does not match its
  SHA-256 (https://github.com/huggingface/huggingface_hub/issues/3643), and
  checksum validation on download is still a feature request
  (https://github.com/huggingface/huggingface_hub/issues/2364). The
  hardening guides say the same: exclude scripts, and verify SHA-256 against
  the Hub's metadata yourself
  (https://www.systemshardening.com/articles/ai-landscape/huggingface-fake-repo-attack-defence/).
- There is no official fp16 `bge-reranker-v2-m3` at the pinned revision
  (the only fp16 safetensors is an unmerged pull request,
  https://huggingface.co/BAAI/bge-reranker-v2-m3/blob/refs%2Fpr%2F36/model.safetensors);
  GGUF quantisations exist for llama.cpp
  (https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF) and are a
  different runtime. The product quantises to int8 at load
  (`2026-09-07-a-reranker-that-never-finished.md`); the 2.27 GB on disk
  stays.

**What the product already has.** `benchmark/run_retrieval_v2.py`'s
`prefetch_models` does exactly this for the benchmark: an isolated cache
root, `allow_download=True` for one explicit step, an acquisition receipt,
and every later run offline. The benchmark loads through `transformers`
with `revision=` and `local_files_only=False`, which is `snapshot_download`
underneath. `_scan_artifacts` in the generation catalog is the hashing
discipline: declared size and SHA-256, bounded read, refusal on mismatch.

**Proposal (needs the owner's yes: it adds a network step to the install
and an environment fact — where the weights live).**
1. One explicit, idempotent operator command, `scripts/install_models.py`:
   for each of the two pinned models, `snapshot_download` at the pinned
   commit with `allow_patterns` limited to `config.json`,
   `model.safetensors`, `tokenizer*`, `sentencepiece*`, `special_tokens*`,
   `1_Pooling/*`; then SHA-256 of `model.safetensors` checked against the
   digest recorded in `scripts/embedding_model.py` and `scripts/reranker.py`
   beside the revision (the digests this machine's blobs carry, named below).
   Prints what it fetched, what it verified, how many bytes; exit 1 on a
   mismatch, leaving the file removed.
2. The installer (`install_control.py` path that already installs the
   units) runs it once; `doctor` names `model_weights_missing` with that
   command when either model is absent, so the state is visible, not
   silent. `sync_memory` keeps its contract and does not download.
3. Nothing changes in the read path: it stays local-only.
4. Not done: a smaller reranker on disk (no official fp16; changing the
   model changes the approved matrix), a background download in the nightly
   (the owner's machine should not fetch 2.7 GB at 3 a.m. without being
   asked), and a mirror or proxy.

**Files it would touch.** `scripts/install_models.py` (new),
`scripts/embedding_model.py`, `scripts/reranker.py`, `scripts/doctor.py`,
`scripts/install_control.py`, `docs/USER-GUIDE.md`, `docs/STRUCTURE.md`,
`CHANGELOG.md`, tests for each.

**Digests on this machine (the pinned blobs, to be recorded in code once
the owner says yes):** recorded below.
- `models--intfloat--multilingual-e5-small` model.safetensors sha256 `1a55775f53449dac10a2bcbc312469fac40b96d53198c407081a831f81c98477` (470641600 bytes)
- `models--BAAI--bge-reranker-v2-m3` model.safetensors sha256 `d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286` (2271071852 bytes)

**Decided by the owner 2026-09-10 («да, делай») and implemented the same
day.** One change to the proposal: the base install does not include the
semantic extra, so the installer cannot fetch weights before the library
that reads them exists. Hence three places, not one: `install.sh` and
`install.ps1` run the command and treat "library absent" as information;
the nightly pass runs it after the index work every night (a no-op once
the weights are present, 30-minute bound); `doctor` names the missing
weights and the command. `install_control.py` is untouched — the install
transaction does not own a network step.

**Files.** `scripts/install_models.py` (new), `scripts/embedding_model.py`,
`scripts/reranker.py`, `scripts/doctor.py`, `scripts/scheduled_nightly.py`,
`install.sh`, `install.ps1`, `tests/test_install_models.py` (new),
`docs/USER-GUIDE.md`, `docs/STRUCTURE.md`, `CHANGELOG.md`,
`docs/ISSUES-2026-09-10.md`.
