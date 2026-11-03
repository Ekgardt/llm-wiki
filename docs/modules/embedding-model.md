# Embedding model identity and text prefixes

`embedding_model` owns the existing model/revision, artifact identity, dimensions,
read/retired file lists and query/passage prefixes. It performs only text prefixing;
model downloads, verified installation, tokenization and inference stay with their
callers. No model version, digest, dimensionality or preprocessing changes here.
Keep scripts/embedding_model.py byte-identical.

The pinned model card states 384 dimensions and role-specific query/passage
prefixes. Tests protect exact pins and prefix behavior on empty and Unicode text,
input-list preservation and the existing no-instruction branch. Installer/encoder
compatibility is tested separately with real consumer contracts; this packet
makes no retrieval-quality or inference-performance claim.
Run `pytest tests/modules/embedding_model tests/test_install_models.py -q`.
See the existing-leaf research for the pinned upstream source and limitations.

Shared canonical contract: [Maintenance qualification](context-tooling.md).
