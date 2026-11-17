"""Shared durability and validation primitives for reliable memory operations."""

from __future__ import annotations

import ctypes as ctypes
import os as os
import sqlite3 as sqlite3
import subprocess as subprocess
import time as time

from bounded_io import IO_CHUNK_BYTES as IO_CHUNK_BYTES
from llm_wiki.formats.canonical import (
    canonical_json_bytes as canonical_json_bytes,
)
from llm_wiki.formats.canonical import (
    sha256_bytes as sha256_bytes,
)
from llm_wiki.formats.schema import (
    SchemaValidationError as SchemaValidationError,
)
from llm_wiki.formats.schema import (
    validate_schema as validate_schema,
)
from llm_wiki.formats.schema import (
    validate_schema_object as validate_schema_object,
)
from llm_wiki.reliability_defaults import DEFAULTS as DEFAULTS
from llm_wiki.reliability_defaults import MAX_CAPTURE_DECISION_BYTES as MAX_CAPTURE_DECISION_BYTES
from llm_wiki.reliability_defaults import ReliableMemoryDefaults as ReliableMemoryDefaults
from llm_wiki.restricted_paths import is_posix_relative_text as is_posix_relative_text
from llm_wiki.restricted_paths import require_inside_a_root as require_inside_a_root
from llm_wiki.restricted_paths import require_normalized_relative as require_normalized_relative
from llm_wiki.restricted_paths import require_posix_relative_text as require_posix_relative_text
from llm_wiki.restricted_paths import restricted_relative_path as restricted_relative_path
from llm_wiki.restricted_paths import under_root as under_root
from llm_wiki.runtime_contracts import (
    MetadataDurabilityUnavailable as MetadataDurabilityUnavailable,
)
from llm_wiki.runtime_contracts import RuntimeFileIdentity as RuntimeFileIdentity
from llm_wiki.runtime_contracts import UnsafeStateRoot as UnsafeStateRoot
from llm_wiki.runtime_environment import CLOUD_DIRECTORY_NAMES as CLOUD_DIRECTORY_NAMES
from llm_wiki.runtime_environment import chmod_or_warn as chmod_or_warn
from llm_wiki.runtime_environment import decode_mount_path as decode_mount_path
from llm_wiki.runtime_environment import harden_runtime_owner_only as harden_runtime_owner_only
from llm_wiki.runtime_environment import has_reparse_attribute as has_reparse_attribute
from llm_wiki.runtime_environment import is_network_filesystem as is_network_filesystem
from llm_wiki.runtime_environment import known_network_path as known_network_path
from llm_wiki.runtime_environment import (
    owner_permissions_supported as owner_permissions_supported,
)
from llm_wiki.runtime_environment import parse_darwin_mounts as parse_darwin_mounts
from llm_wiki.runtime_environment import parse_posix_mounts as parse_posix_mounts
from llm_wiki.runtime_environment import path_is_under as path_is_under
from llm_wiki.runtime_environment import platform_system as platform_system
from llm_wiki.runtime_environment import posix_mount_table as posix_mount_table
from llm_wiki.runtime_environment import posix_network_path as posix_network_path
from llm_wiki.runtime_environment import query_darwin_mounts as query_darwin_mounts
from llm_wiki.runtime_environment import read_posix_mount_data as read_posix_mount_data
from llm_wiki.runtime_environment import require_applied_mode as require_applied_mode
from llm_wiki.runtime_environment import (
    require_local_non_reparse_root as require_local_non_reparse_root,
)
from llm_wiki.runtime_environment import require_owner_only_file as require_owner_only_file
from llm_wiki.runtime_environment import require_windows_owner_only as require_windows_owner_only
from llm_wiki.runtime_environment import set_owner_only as set_owner_only
from llm_wiki.runtime_environment import warn_if_cloud_synchronized as warn_if_cloud_synchronized
from llm_wiki.runtime_environment import windows_network_anchor as windows_network_anchor
from llm_wiki.runtime_environment import windows_reparse_point as windows_reparse_point
from llm_wiki.runtime_fingerprints import digest_runtime_descriptor as digest_runtime_descriptor
from llm_wiki.runtime_fingerprints import fingerprint_metadata as fingerprint_metadata
from llm_wiki.runtime_fingerprints import fingerprint_regular_file as fingerprint_regular_file
from llm_wiki.runtime_fingerprints import fingerprint_runtime_file as fingerprint_runtime_file
from llm_wiki.runtime_fingerprints import (
    require_fingerprint_metadata as require_fingerprint_metadata,
)
from llm_wiki.runtime_fingerprints import verified_runtime_digest as verified_runtime_digest
from llm_wiki.runtime_publication import adopted_or_duplicate as adopted_or_duplicate
from llm_wiki.runtime_publication import discard_staged as discard_staged
from llm_wiki.runtime_publication import durable_publish_file as durable_publish_file
from llm_wiki.runtime_publication import fsync_parent_on_posix as fsync_parent_on_posix
from llm_wiki.runtime_publication import link_then_unlink as link_then_unlink
from llm_wiki.runtime_publication import move_into_place as move_into_place
from llm_wiki.runtime_publication import move_posix as move_posix
from llm_wiki.runtime_publication import move_windows as move_windows
from llm_wiki.runtime_publication import publication_digest as publication_digest
from llm_wiki.runtime_publication import publish_runtime_file as publish_runtime_file
from llm_wiki.runtime_publication import publish_staged as publish_staged
from llm_wiki.runtime_publication import replace_in_place as replace_in_place
from llm_wiki.runtime_publication import (
    require_create_only_carries_no_evidence as require_create_only_carries_no_evidence,
)
from llm_wiki.runtime_publication import require_distinct_siblings as require_distinct_siblings
from llm_wiki.runtime_publication import (
    require_publication_arguments as require_publication_arguments,
)
from llm_wiki.runtime_publication import (
    require_replacement_carries_evidence as require_replacement_carries_evidence,
)
from llm_wiki.runtime_publication import (
    require_replacement_evidence as require_replacement_evidence,
)
from llm_wiki.runtime_publication import require_replacement_shape as require_replacement_shape
from llm_wiki.runtime_publication import require_runtime_arguments as require_runtime_arguments
from llm_wiki.runtime_publication import require_runtime_mode as require_runtime_mode
from llm_wiki.runtime_publication import require_runtime_payload as require_runtime_payload
from llm_wiki.runtime_publication import require_unchanged_identity as require_unchanged_identity
from llm_wiki.runtime_publication import resolved_parents as resolved_parents
from llm_wiki.runtime_publication import (
    resolved_runtime_destination as resolved_runtime_destination,
)
from llm_wiki.runtime_publication import resolved_sibling_pair as resolved_sibling_pair
from llm_wiki.runtime_publication import settled_publication as settled_publication
from llm_wiki.runtime_publication import unlink_staged as unlink_staged
from llm_wiki.runtime_publication import (
    verify_published_runtime_permissions as verify_published_runtime_permissions,
)
from llm_wiki.runtime_publication import write_all as write_all
from llm_wiki.runtime_publication import write_staged_bytes as write_staged_bytes
from llm_wiki.runtime_records import (
    capture_operational_database_identity as capture_operational_database_identity,
)
from llm_wiki.runtime_records import capture_runtime_file_identity as capture_runtime_file_identity
from llm_wiki.runtime_records import confirm_runtime_identity as confirm_runtime_identity
from llm_wiki.runtime_records import contained_runtime_metadata as contained_runtime_metadata
from llm_wiki.runtime_records import posix_runtime_identity_names as posix_runtime_identity_names
from llm_wiki.runtime_records import read_runtime_bytes as read_runtime_bytes
from llm_wiki.runtime_records import require_bounded_regular_file as require_bounded_regular_file
from llm_wiki.runtime_records import require_file_size_budget as require_file_size_budget
from llm_wiki.runtime_records import (
    require_nonnegative_file_budget as require_nonnegative_file_budget,
)
from llm_wiki.runtime_records import (
    require_runtime_parent_containment as require_runtime_parent_containment,
)
from llm_wiki.runtime_records import require_stable_read as require_stable_read
from llm_wiki.runtime_records import (
    runtime_identity_from_metadata as runtime_identity_from_metadata,
)
from llm_wiki.runtime_records import validate_operational_db_file as validate_operational_db_file
from llm_wiki.runtime_records import validate_runtime_file as validate_runtime_file
from llm_wiki.runtime_records import validated_runtime_metadata as validated_runtime_metadata
from llm_wiki.runtime_records import (
    windows_runtime_identity_names as windows_runtime_identity_names,
)
from llm_wiki.runtime_sync import close_windows_handle as close_windows_handle
from llm_wiki.runtime_sync import file_sync_mode as file_sync_mode
from llm_wiki.runtime_sync import fsync_directory as fsync_directory
from llm_wiki.runtime_sync import fsync_directory_posix as fsync_directory_posix
from llm_wiki.runtime_sync import fsync_directory_windows as fsync_directory_windows
from llm_wiki.runtime_sync import fsync_file as fsync_file
from llm_wiki.runtime_sync import metadata_durability_error as metadata_durability_error
from llm_wiki.runtime_sync import opened_directory_descriptor as opened_directory_descriptor
from llm_wiki.runtime_sync import sync_runtime_directory as sync_runtime_directory
from llm_wiki.sqlite_contracts import MigrationStatement as MigrationStatement
from llm_wiki.sqlite_contracts import OperationalDatabaseContract as OperationalDatabaseContract
from llm_wiki.sqlite_contracts import (
    OperationalDatabaseContractError as OperationalDatabaseContractError,
)
from llm_wiki.sqlite_contracts import quote_sqlite_identifier as quote_sqlite_identifier
from llm_wiki.sqlite_contracts import require_migration_text as require_migration_text
from llm_wiki.sqlite_contracts import require_positive_int as require_positive_int
from llm_wiki.sqlite_deadlines import (
    OPERATIONAL_PROGRESS_VM_STEPS as OPERATIONAL_PROGRESS_VM_STEPS,
)
from llm_wiki.sqlite_deadlines import (
    configure_operational_deadline as configure_operational_deadline,
)
from llm_wiki.sqlite_deadlines import operational_busy_ms as operational_busy_ms
from llm_wiki.sqlite_deadlines import operational_deadline_active as operational_deadline_active
from llm_wiki.sqlite_deadlines import refresh_operational_deadline as refresh_operational_deadline
from llm_wiki.sqlite_io import JOURNAL_REPLAY_BUSY_MS as JOURNAL_REPLAY_BUSY_MS
from llm_wiki.sqlite_io import OPERATIONAL_SCAN_BATCH_ROWS as OPERATIONAL_SCAN_BATCH_ROWS
from llm_wiki.sqlite_io import cleanup_lock_probe as cleanup_lock_probe
from llm_wiki.sqlite_io import close_probe_connections as close_probe_connections
from llm_wiki.sqlite_io import (
    configure_operational_connection as configure_operational_connection,
)
from llm_wiki.sqlite_io import contained_lock_probe as contained_lock_probe
from llm_wiki.sqlite_io import hot_journal as hot_journal
from llm_wiki.sqlite_io import initialize_probe_directory as initialize_probe_directory
from llm_wiki.sqlite_io import open_operational_db as open_operational_db
from llm_wiki.sqlite_io import open_readonly_operational_db as open_readonly_operational_db
from llm_wiki.sqlite_io import opened_probe as opened_probe
from llm_wiki.sqlite_io import opened_readonly_operational_db as opened_readonly_operational_db
from llm_wiki.sqlite_io import operational_db_identity as operational_db_identity
from llm_wiki.sqlite_io import (
    prepare_local_database_directory as prepare_local_database_directory,
)
from llm_wiki.sqlite_io import probe_directory as probe_directory
from llm_wiki.sqlite_io import probe_directory_identity as probe_directory_identity
from llm_wiki.sqlite_io import readonly_write_refusal as readonly_write_refusal
from llm_wiki.sqlite_io import remove_probe_files as remove_probe_files
from llm_wiki.sqlite_io import replayed_hot_journal as replayed_hot_journal
from llm_wiki.sqlite_io import (
    require_operational_open_arguments as require_operational_open_arguments,
)
from llm_wiki.sqlite_io import require_probe_component as require_probe_component
from llm_wiki.sqlite_io import require_probe_root_chain as require_probe_root_chain
from llm_wiki.sqlite_io import require_replayed_journal as require_replayed_journal
from llm_wiki.sqlite_io import require_same_probe_directory as require_same_probe_directory
from llm_wiki.sqlite_io import run_lock_probe as run_lock_probe
from llm_wiki.sqlite_io import second_writer_is_blocked as second_writer_is_blocked
from llm_wiki.sqlite_io import sqlite_directory_lock_probe as sqlite_directory_lock_probe
from llm_wiki.sqlite_io import sqlite_lock_probe as sqlite_lock_probe
from llm_wiki.sqlite_io import streamed_rows as streamed_rows
from llm_wiki.sqlite_io import validate_database_directory as validate_database_directory
from llm_wiki.sqlite_io import validate_state_root as validate_state_root
from llm_wiki.sqlite_migrations import (
    apply_one_migration_statement as apply_one_migration_statement,
)
from llm_wiki.sqlite_migrations import migration_incomplete as migration_incomplete
from llm_wiki.sqlite_migrations import require_final_invariant as require_final_invariant
from llm_wiki.sqlite_migrations import (
    require_unique_statement_names as require_unique_statement_names,
)
from llm_wiki.sqlite_migrations import run_resumable_migration as run_resumable_migration
from llm_wiki.sqlite_pragmas import apply_operational_pragmas as apply_operational_pragmas
from llm_wiki.sqlite_pragmas import (
    apply_readonly_operational_pragmas as apply_readonly_operational_pragmas,
)
from llm_wiki.sqlite_pragmas import initialized_contract_identity as initialized_contract_identity
from llm_wiki.sqlite_pragmas import pragma_integer as pragma_integer
from llm_wiki.sqlite_pragmas import require_pragma as require_pragma
from llm_wiki.sqlite_pragmas import (
    validate_or_initialize_operational_contract as validate_or_initialize_operational_contract,
)
from llm_wiki.sqlite_transactions import begin_immediate as begin_immediate
