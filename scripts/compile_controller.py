"""Durable external-call boundary for the approved compile controller.

The candidate CLI/MCP/nightly paths use this controller. The shared client
prepares protected calls; semantic callers own work identity and publication.
Installation and full dependency/recovery qualification remain unfinished.

The code lives in the ``llm_wiki.compile_controller_*`` concern modules; this
facade keeps the documented import path and its historical names.
"""
from __future__ import annotations

from llm_wiki.compile_answer_records import COMPLETED_REFUSALS as COMPLETED_REFUSALS
from llm_wiki.compile_answer_records import answer_event as answer_event
from llm_wiki.compile_answer_records import encoded_answer as encoded_answer
from llm_wiki.compile_call_runtime import CompileDelivery as CompileDelivery
from llm_wiki.compile_call_runtime import ProviderOutcomeUnknown as ProviderOutcomeUnknown
from llm_wiki.compile_call_runtime import authorize_model_repeat as authorize_model_repeat
from llm_wiki.compile_call_runtime import dispatch_event as dispatch_event
from llm_wiki.compile_call_runtime import require_checkpoint as require_checkpoint
from llm_wiki.compile_call_runtime import resolve_model_call as resolve_model_call
from llm_wiki.compile_call_runtime import run_model_call as run_model_call
from llm_wiki.compile_controller_archive import ARCHIVE_JSON_FIELDS as ARCHIVE_JSON_FIELDS
from llm_wiki.compile_controller_archive import ARCHIVE_SCHEMA as ARCHIVE_SCHEMA
from llm_wiki.compile_controller_archive import ArchiveJsonArray as ArchiveJsonArray
from llm_wiki.compile_controller_archive import SnapshotArchiveWriter as SnapshotArchiveWriter
from llm_wiki.compile_controller_archive import SnapshotJsonArray as SnapshotJsonArray
from llm_wiki.compile_controller_archive import archive_record_value as archive_record_value
from llm_wiki.compile_controller_archive import blob_digest as blob_digest
from llm_wiki.compile_controller_archive import compile_inputs_digest as compile_inputs_digest
from llm_wiki.compile_controller_archive import put_snapshot_json as put_snapshot_json
from llm_wiki.compile_controller_archive import snapshot_content_chunks as snapshot_content_chunks
from llm_wiki.compile_controller_archive import snapshot_json as snapshot_json
from llm_wiki.compile_controller_archive import write_archive_snapshot as write_archive_snapshot
from llm_wiki.compile_controller_batch_admission import begin_compile_batch as begin_compile_batch
from llm_wiki.compile_controller_batch_admission import (
    save_compile_batch_context as save_compile_batch_context,
)
from llm_wiki.compile_controller_batches import execute_compile_tree as execute_compile_tree
from llm_wiki.compile_controller_batches import (
    execute_planned_compile_batch as execute_planned_compile_batch,
)
from llm_wiki.compile_controller_batches import (
    execute_planned_compile_tree as execute_planned_compile_tree,
)
from llm_wiki.compile_controller_batches import (
    open_compile_batch_context as open_compile_batch_context,
)
from llm_wiki.compile_controller_batches import (
    open_prepared_compile_batch as open_prepared_compile_batch,
)
from llm_wiki.compile_controller_calls import CompileReplay as CompileReplay
from llm_wiki.compile_controller_calls import run_candidate_call as run_candidate_call
from llm_wiki.compile_controller_capture import (
    CompileCapturePreparation as CompileCapturePreparation,
)
from llm_wiki.compile_controller_capture import admit_compile_capture as admit_compile_capture
from llm_wiki.compile_controller_capture import read_compile_execution as read_compile_execution
from llm_wiki.compile_controller_capture import require_capture_vault as require_capture_vault
from llm_wiki.compile_controller_capture import save_compile_capture as save_compile_capture
from llm_wiki.compile_controller_capture import save_compile_inputs as save_compile_inputs
from llm_wiki.compile_controller_capture_check import (
    read_admitted_capture_root as read_admitted_capture_root,
)
from llm_wiki.compile_controller_capture_check import (
    validate_capture_archive as validate_capture_archive,
)
from llm_wiki.compile_controller_context_delta import (
    read_compile_context_delta as read_compile_context_delta,
)
from llm_wiki.compile_controller_context_delta import (
    write_compile_context_delta as write_compile_context_delta,
)
from llm_wiki.compile_controller_dependencies import CompileDependencies as CompileDependencies
from llm_wiki.compile_controller_dependencies import (
    capture_compile_dependencies as capture_compile_dependencies,
)
from llm_wiki.compile_controller_dependencies import (
    compile_configuration_dependency_manifest as compile_configuration_dependency_manifest,
)
from llm_wiki.compile_controller_dependencies import (
    compile_dependency_digest as compile_dependency_digest,
)
from llm_wiki.compile_controller_dependencies import (
    compile_dependency_file as compile_dependency_file,
)
from llm_wiki.compile_controller_dependencies import (
    compile_dependency_manifest as compile_dependency_manifest,
)
from llm_wiki.compile_controller_dependencies import (
    compile_execution_dependency_manifest as compile_execution_dependency_manifest,
)
from llm_wiki.compile_controller_dependencies import (
    compile_package_dependency_manifest as compile_package_dependency_manifest,
)
from llm_wiki.compile_controller_dependencies import (
    compile_product_dependency_manifest as compile_product_dependency_manifest,
)
from llm_wiki.compile_controller_dependencies import (
    compile_runtime_dependency_manifest as compile_runtime_dependency_manifest,
)
from llm_wiki.compile_controller_dependencies import (
    compile_runtime_library as compile_runtime_library,
)
from llm_wiki.compile_controller_dependencies import compile_work_identity as compile_work_identity
from llm_wiki.compile_controller_preflight import PlannedCompileBatch as PlannedCompileBatch
from llm_wiki.compile_controller_preflight import iter_compile_preflight as iter_compile_preflight
from llm_wiki.compile_controller_preflight import (
    preflight_source_records as preflight_source_records,
)
from llm_wiki.compile_controller_preflight import save_compile_preflight as save_compile_preflight
from llm_wiki.compile_controller_publication import (
    confirm_compile_publication as confirm_compile_publication,
)
from llm_wiki.compile_controller_publication import (
    execute_compile_publication as execute_compile_publication,
)
from llm_wiki.compile_controller_publication import publication_content as publication_content
from llm_wiki.compile_controller_publication import (
    read_compile_publication as read_compile_publication,
)
from llm_wiki.compile_controller_publication import (
    save_compile_publication as save_compile_publication,
)
from llm_wiki.compile_controller_reader import ArchiveRecords as ArchiveRecords
from llm_wiki.compile_controller_reader import CompileInputArchive as CompileInputArchive
from llm_wiki.compile_controller_reader import OriginalDigestProof as OriginalDigestProof
from llm_wiki.compile_controller_reader import compile_capture_digest as compile_capture_digest
from llm_wiki.compile_controller_reader import open_compile_inputs as open_compile_inputs
from llm_wiki.compile_controller_reader import restored_archive_record as restored_archive_record
from llm_wiki.compile_controller_reader import write_compile_capture as write_compile_capture
from llm_wiki.compile_controller_repair import RepairCompileBatch as RepairCompileBatch
from llm_wiki.compile_controller_repair import (
    begin_compile_repair_batch as begin_compile_repair_batch,
)
from llm_wiki.compile_controller_repair import confirm_compile_repair as confirm_compile_repair
from llm_wiki.compile_controller_repair import (
    execute_repair_compile_batch as execute_repair_compile_batch,
)
from llm_wiki.compile_controller_repair import read_compile_repair as read_compile_repair
from llm_wiki.compile_controller_repair import save_compile_repair as save_compile_repair
from llm_wiki.compile_controller_work import begin_compile_work as begin_compile_work
from llm_wiki.compile_controller_work import compile_root_details as compile_root_details
from llm_wiki.compile_controller_work import confirm_compile_work as confirm_compile_work
from llm_wiki.compile_controller_work import execute_compile_work as execute_compile_work
from llm_wiki.compile_controller_work import load_compile_inputs as load_compile_inputs
