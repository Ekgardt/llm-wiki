"""Fenced SQLite queue for deferred memory-pipeline work."""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import math
import multiprocessing
import os
import random
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from markdown_transaction import IntentFence
    from operational_ownership import OwnerLease

from reliable_memory import (
    DEFAULTS,
    MigrationStatement,
    OperationalDatabaseContract,
    OperationalDatabaseContractError,
    _set_owner_only,
    begin_immediate,
    canonical_json_bytes,
    fsync_directory,
    fsync_file,
    open_operational_db,
    open_readonly_operational_db,
    read_runtime_bytes,
    restricted_relative_path,
    run_resumable_migration,
    sha256_bytes,
    validate_schema,
)
from secret_redact import redact_secrets

_STATES = ("ready", "leased", "blocked", "succeeded", "dead", "cancelled")
_TERMINAL_STATES = ("succeeded", "dead", "cancelled")
_PERMANENT_CODES = {"invalid_input", "unsupported_version"}
_MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_MAX_EXPORT_METADATA_BYTES = 64 * 1024 * 1024
_MAX_LEGACY_RECORD_BYTES = 16 * 1024 * 1024
_MAX_RUNTIME_ATTEMPTS = 100
_MAX_MARKER_BYTES = 64
_MAX_QUEUE_PAYLOAD_BYTES = 1024 * 1024
_MAX_QUEUE_DEPTH = 32
_MAX_QUEUE_STRING_BYTES = 256 * 1024
_MAX_QUEUE_CONTAINER_MEMBERS = 1024
_QUEUE_V3_CONTRACT = OperationalDatabaseContract(application_id=0x4C575133)
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "pass",
    "passwd",
    "passphrase",
    "password",
    "private_key",
    "secret",
    "set_cookie",
    "token",
}
_CAPTURE_TERMINAL_DISPOSITION_FIELDS = {
    "markdown_committed": {
        "kind",
        "transaction_id",
        "operation_id",
        "decision_sha256",
        "outputs",
    },
    "no_durable_content": {"kind", "decision_sha256"},
}

_QUEUE_V3_TABLE_SQL = (
    (
        "tasks",
        """CREATE TABLE tasks (
            id TEXT NOT NULL PRIMARY KEY CHECK (length(CAST(id AS BLOB)) BETWEEN 1 AND 256),
            kind TEXT NOT NULL CHECK (length(CAST(kind AS BLOB)) BETWEEN 1 AND 64),
            handler_version INTEGER NOT NULL CHECK (handler_version BETWEEN 1 AND 2147483647),
            payload_blob BLOB NOT NULL CHECK (length(payload_blob) <= 1048576),
            input_hash TEXT NOT NULL CHECK (
                length(input_hash) = 64 AND input_hash NOT GLOB '*[^0-9a-f]*'
            ),
            dedupe_key TEXT CHECK (
                dedupe_key IS NULL OR length(CAST(dedupe_key AS BLOB)) BETWEEN 1 AND 512
            ),
            state TEXT NOT NULL CHECK (state IN (
                'ready','leased','blocked','succeeded','dead','cancelled',
                'quarantine_pending','quarantined','purge_pending'
            )),
            priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 100),
            last_attempt_at TEXT,
            lease_owner TEXT CHECK (
                lease_owner IS NULL OR length(CAST(lease_owner AS BLOB)) <= 256
            ),
            lease_token TEXT CHECK (
                lease_token IS NULL OR length(CAST(lease_token AS BLOB)) <= 256
            ),
            lease_expires_at TEXT,
            lease_heartbeat_at TEXT,
            attempt_started_at TEXT,
            error_code TEXT CHECK (
                error_code IS NULL OR length(CAST(error_code AS BLOB)) BETWEEN 1 AND 64
            ),
            blocked_capability TEXT CHECK (
                blocked_capability IS NULL OR length(CAST(blocked_capability AS BLOB)) <= 128
            ),
            result_reference TEXT CHECK (
                result_reference IS NULL OR length(CAST(result_reference AS BLOB)) <= 4096
            ),
            result_sha256 TEXT CHECK (
                result_sha256 IS NULL OR (
                    length(result_sha256) = 64 AND result_sha256 NOT GLOB '*[^0-9a-f]*'
                )
            ),
            result_operation_id TEXT CHECK (
                result_operation_id IS NULL OR length(CAST(result_operation_id AS BLOB)) <= 4096
            ),
            redrive_of TEXT REFERENCES tasks(id),
            lineage_generation INTEGER NOT NULL DEFAULT 0 CHECK (lineage_generation >= 0)
        )""",
    ),
    (
        "attempt_history",
        """CREATE TABLE attempt_history (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            attempt INTEGER NOT NULL CHECK (attempt BETWEEN 1 AND 100),
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN (
                'succeeded','failed','blocked','cancelled','lease_expired'
            )),
            error_code TEXT CHECK (
                error_code IS NULL OR length(CAST(error_code AS BLOB)) BETWEEN 1 AND 64
            )
        )""",
    ),
    (
        "source_fences",
        """CREATE TABLE source_fences (
            logical_path TEXT NOT NULL CHECK (length(CAST(logical_path AS BLOB)) BETWEEN 1 AND 4096),
            source_digest TEXT NOT NULL CHECK (
                length(source_digest) = 64 AND source_digest NOT GLOB '*[^0-9a-f]*'
            ),
            token TEXT NOT NULL UNIQUE CHECK (length(CAST(token AS BLOB)) BETWEEN 1 AND 256),
            owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
            owner_start_identity TEXT NOT NULL CHECK (
                length(CAST(owner_start_identity AS BLOB)) BETWEEN 1 AND 512
            ),
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY(logical_path, source_digest)
        )""",
    ),
    (
        "source_failures",
        """CREATE TABLE source_failures (
            logical_path TEXT NOT NULL CHECK (length(CAST(logical_path AS BLOB)) BETWEEN 1 AND 4096),
            source_digest TEXT NOT NULL CHECK (
                length(source_digest) = 64 AND source_digest NOT GLOB '*[^0-9a-f]*'
            ),
            error_code TEXT NOT NULL CHECK (length(CAST(error_code AS BLOB)) BETWEEN 1 AND 64),
            producer TEXT NOT NULL CHECK (producer IN ('compile','queue')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY(logical_path, source_digest)
        )""",
    ),
    (
        "task_source_links",
        """CREATE TABLE task_source_links (
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            logical_path TEXT NOT NULL CHECK (length(CAST(logical_path AS BLOB)) BETWEEN 1 AND 4096),
            source_digest TEXT NOT NULL CHECK (
                length(source_digest) = 64 AND source_digest NOT GLOB '*[^0-9a-f]*'
            ),
            PRIMARY KEY(task_id, logical_path, source_digest)
        )""",
    ),
    (
        "queue_ownership",
        """CREATE TABLE queue_ownership (
            actor_id TEXT NOT NULL PRIMARY KEY CHECK (length(CAST(actor_id AS BLOB)) BETWEEN 1 AND 256),
            domain_role TEXT NOT NULL CHECK (domain_role IN ('worker','operator')),
            canonical_role TEXT NOT NULL CHECK (
                canonical_role IN (
                    'queue-worker','queue-operator','repair','compile','doctor','nightly','weekly'
                )
            ),
            canonical_scope TEXT NOT NULL CHECK (
                length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512
            ),
            owner_token TEXT NOT NULL UNIQUE CHECK (length(CAST(owner_token AS BLOB)) BETWEEN 1 AND 256),
            fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
            process_id INTEGER NOT NULL CHECK (process_id > 0),
            process_start_identity TEXT NOT NULL CHECK (
                length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
            ),
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            UNIQUE(canonical_role, canonical_scope),
            CHECK (
                (domain_role = 'worker' AND canonical_role IN (
                    'queue-worker','compile','doctor','nightly','weekly'
                ))
                OR
                (domain_role = 'operator' AND canonical_role IN ('queue-operator','repair'))
            )
        )""",
    ),
    (
        "task_purge_authorizations",
        """CREATE TABLE task_purge_authorizations (
            task_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256
            ),
            mode TEXT NOT NULL CHECK (mode IN ('ordinary','corrupt-lineage','corrupt-parent')),
            operation_id TEXT NOT NULL CHECK (
                length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 4096
            ),
            authorization_digest TEXT NOT NULL CHECK (
                length(authorization_digest) = 64
                AND authorization_digest NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL
        )""",
    ),
    (
        "capture_intents",
        """CREATE TABLE capture_intents (
            intent_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(intent_id) = 64 AND intent_id NOT GLOB '*[^0-9a-f]*'
            ),
            relative_path TEXT NOT NULL CHECK (length(CAST(relative_path AS BLOB)) BETWEEN 1 AND 4096),
            intent_sha256 TEXT NOT NULL CHECK (
                length(intent_sha256) = 64 AND intent_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            byte_size INTEGER NOT NULL CHECK (byte_size BETWEEN 1 AND 1048576),
            publication_state TEXT NOT NULL CHECK (publication_state IN ('pending','ready')),
            updated_at TEXT NOT NULL
        )""",
    ),
    (
        "capture_task_links",
        """CREATE TABLE capture_task_links (
            task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id),
            intent_id TEXT NOT NULL REFERENCES capture_intents(intent_id),
            intent_sha256 TEXT NOT NULL CHECK (
                length(intent_sha256) = 64 AND intent_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            handler_version INTEGER NOT NULL CHECK (handler_version BETWEEN 1 AND 2147483647),
            link_digest TEXT NOT NULL UNIQUE CHECK (
                length(link_digest) = 64 AND link_digest NOT GLOB '*[^0-9a-f]*'
            ),
            created_at TEXT NOT NULL
        )""",
    ),
    (
        "capture_task_link_resolutions",
        """CREATE TABLE capture_task_link_resolutions (
            resolution_digest TEXT NOT NULL PRIMARY KEY CHECK (
                length(resolution_digest) = 64 AND resolution_digest NOT GLOB '*[^0-9a-f]*'
            ),
            task_id TEXT NOT NULL REFERENCES tasks(id),
            supersedes_digest TEXT UNIQUE CHECK (
                supersedes_digest IS NULL OR (
                    length(supersedes_digest) = 64
                    AND supersedes_digest NOT GLOB '*[^0-9a-f]*'
                )
            ),
            observed_json BLOB NOT NULL CHECK (length(observed_json) <= 65536),
            selected_intent_id TEXT REFERENCES capture_intents(intent_id),
            actor_identity TEXT NOT NULL CHECK (length(CAST(actor_identity AS BLOB)) BETWEEN 1 AND 512),
            reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
            created_at TEXT NOT NULL
        )""",
    ),
    (
        "capture_task_link_seals",
        """CREATE TABLE capture_task_link_seals (
            task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id),
            active_digest TEXT NOT NULL CHECK (
                length(active_digest) = 64 AND active_digest NOT GLOB '*[^0-9a-f]*'
            ),
            consumer_kind TEXT NOT NULL CHECK (consumer_kind IN (
                'semantic-decision','transaction','terminal','corrupt-disposition'
            )),
            consumer_id TEXT NOT NULL CHECK (length(CAST(consumer_id AS BLOB)) BETWEEN 1 AND 4096),
            seal_digest TEXT NOT NULL UNIQUE CHECK (
                length(seal_digest) = 64 AND seal_digest NOT GLOB '*[^0-9a-f]*'
            ),
            sealed_at TEXT NOT NULL
        )""",
    ),
    (
        "semantic_decisions",
        """CREATE TABLE semantic_decisions (
            intent_id TEXT NOT NULL REFERENCES capture_intents(intent_id),
            stage TEXT NOT NULL CHECK (stage IN ('flush','feedback','feedback-verify')),
            decision_path TEXT NOT NULL CHECK (length(CAST(decision_path AS BLOB)) BETWEEN 1 AND 4096),
            decision_sha256 TEXT NOT NULL CHECK (
                length(decision_sha256) = 64 AND decision_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
            active_link_digest TEXT NOT NULL CHECK (
                length(active_link_digest) = 64 AND active_link_digest NOT GLOB '*[^0-9a-f]*'
            ),
            publication_state TEXT NOT NULL CHECK (publication_state = 'published'),
            published_at TEXT NOT NULL,
            PRIMARY KEY(intent_id, stage)
        )""",
    ),
    (
        "task_fence_epochs",
        """CREATE TABLE task_fence_epochs (
            task_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256
            ),
            last_epoch INTEGER NOT NULL CHECK (last_epoch >= 0)
        )""",
    ),
    (
        "task_fences",
        """CREATE TABLE task_fences (
            task_id TEXT NOT NULL PRIMARY KEY REFERENCES tasks(id),
            mode TEXT NOT NULL CHECK (mode IN ('worker','queue-operator')),
            token TEXT NOT NULL UNIQUE CHECK (length(CAST(token AS BLOB)) BETWEEN 1 AND 256),
            fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
            canonical_role TEXT NOT NULL CHECK (canonical_role IN (
                'queue-worker','compile','doctor','nightly','weekly','queue-operator','repair'
            )),
            canonical_scope TEXT NOT NULL CHECK (length(CAST(canonical_scope AS BLOB)) BETWEEN 1 AND 512),
            canonical_actor_id TEXT NOT NULL CHECK (length(CAST(canonical_actor_id AS BLOB)) BETWEEN 1 AND 256),
            canonical_owner_token TEXT NOT NULL CHECK (length(CAST(canonical_owner_token AS BLOB)) BETWEEN 1 AND 256),
            canonical_fencing_epoch INTEGER NOT NULL CHECK (canonical_fencing_epoch >= 1),
            process_id INTEGER NOT NULL CHECK (process_id > 0),
            process_start_identity TEXT NOT NULL CHECK (
                length(CAST(process_start_identity AS BLOB)) BETWEEN 1 AND 512
            ),
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            CHECK (
                (mode = 'worker' AND canonical_role IN (
                    'queue-worker','compile','doctor','nightly','weekly'
                ))
                OR
                (mode = 'queue-operator' AND canonical_role IN ('queue-operator','repair'))
            )
        )""",
    ),
    (
        "corrupt_export_operations",
        """CREATE TABLE corrupt_export_operations (
            operation_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 4096
            ),
            task_id TEXT NOT NULL UNIQUE CHECK (length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256),
            disposition_key TEXT NOT NULL UNIQUE CHECK (
                length(disposition_key) = 64 AND disposition_key NOT GLOB '*[^0-9a-f]*'
            ),
            task_fence_token_digest TEXT NOT NULL CHECK (
                length(task_fence_token_digest) = 64
                AND task_fence_token_digest NOT GLOB '*[^0-9a-f]*'
            ),
            task_fence_epoch INTEGER NOT NULL CHECK (task_fence_epoch >= 1),
            intent_fence_digest TEXT CHECK (
                intent_fence_digest IS NULL OR (
                    length(intent_fence_digest) = 64
                    AND intent_fence_digest NOT GLOB '*[^0-9a-f]*'
                )
            ),
            raw_sha256 TEXT NOT NULL CHECK (length(raw_sha256) = 64 AND raw_sha256 NOT GLOB '*[^0-9a-f]*'),
            history_sha256 TEXT NOT NULL CHECK (length(history_sha256) = 64 AND history_sha256 NOT GLOB '*[^0-9a-f]*'),
            metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256) = 64 AND metadata_sha256 NOT GLOB '*[^0-9a-f]*'),
            lineage_generation INTEGER NOT NULL CHECK (lineage_generation >= 0),
            cursor_task_id TEXT NOT NULL DEFAULT '' CHECK (length(CAST(cursor_task_id AS BLOB)) <= 256),
            link_count INTEGER NOT NULL DEFAULT 0 CHECK (link_count >= 0),
            page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
            rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
            state TEXT NOT NULL CHECK (state IN ('exporting','manifested','disposed')),
            actor_identity TEXT NOT NULL CHECK (length(CAST(actor_identity AS BLOB)) BETWEEN 1 AND 512),
            reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ),
    (
        "corrupt_export_pages",
        """CREATE TABLE corrupt_export_pages (
            operation_id TEXT NOT NULL REFERENCES corrupt_export_operations(operation_id),
            page_number INTEGER NOT NULL CHECK (page_number >= 1),
            first_task_id TEXT NOT NULL CHECK (length(CAST(first_task_id AS BLOB)) BETWEEN 1 AND 256),
            last_task_id TEXT NOT NULL CHECK (length(CAST(last_task_id AS BLOB)) BETWEEN 1 AND 256),
            link_count INTEGER NOT NULL CHECK (link_count BETWEEN 1 AND 1000),
            page_sha256 TEXT NOT NULL CHECK (length(page_sha256) = 64 AND page_sha256 NOT GLOB '*[^0-9a-f]*'),
            rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
            PRIMARY KEY(operation_id, page_number)
        )""",
    ),
    (
        "corrupt_dispositions",
        """CREATE TABLE corrupt_dispositions (
            task_id TEXT NOT NULL PRIMARY KEY CHECK (length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256),
            operation_id TEXT NOT NULL UNIQUE REFERENCES corrupt_export_operations(operation_id),
            package_path TEXT NOT NULL CHECK (length(CAST(package_path AS BLOB)) BETWEEN 1 AND 4096),
            manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
            disposition_sha256 TEXT NOT NULL CHECK (length(disposition_sha256) = 64 AND disposition_sha256 NOT GLOB '*[^0-9a-f]*'),
            active_link_digest TEXT CHECK (
                active_link_digest IS NULL OR (
                    length(active_link_digest) = 64
                    AND active_link_digest NOT GLOB '*[^0-9a-f]*'
                )
            ),
            disposed_at TEXT NOT NULL
        )""",
    ),
    (
        "corrupt_package_supersession_operations",
        """CREATE TABLE corrupt_package_supersession_operations (
            operation_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 4096
            ),
            package_key TEXT NOT NULL UNIQUE CHECK (
                length(package_key) = 64 AND package_key NOT GLOB '*[^0-9a-f]*'
            ),
            package_path TEXT NOT NULL CHECK (length(CAST(package_path AS BLOB)) BETWEEN 1 AND 4096),
            cursor_name TEXT NOT NULL DEFAULT '' CHECK (
                length(CAST(cursor_name AS BLOB)) <= 256
            ),
            file_count INTEGER NOT NULL DEFAULT 0 CHECK (file_count >= 0),
            page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
            rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
            state TEXT NOT NULL CHECK (state IN ('scanning','disposed')),
            actor_identity TEXT NOT NULL CHECK (length(CAST(actor_identity AS BLOB)) BETWEEN 1 AND 512),
            reason TEXT NOT NULL CHECK (length(CAST(reason AS BLOB)) BETWEEN 1 AND 4096),
            chosen_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ),
    (
        "corrupt_package_supersession_pages",
        """CREATE TABLE corrupt_package_supersession_pages (
            operation_id TEXT NOT NULL REFERENCES corrupt_package_supersession_operations(operation_id),
            page_number INTEGER NOT NULL CHECK (page_number >= 1),
            first_name TEXT NOT NULL CHECK (
                length(CAST(first_name AS BLOB)) BETWEEN 1 AND 256
            ),
            last_name TEXT NOT NULL CHECK (
                length(CAST(last_name AS BLOB)) BETWEEN 1 AND 256
            ),
            file_count INTEGER NOT NULL CHECK (file_count BETWEEN 1 AND 1000),
            page_sha256 TEXT NOT NULL CHECK (length(page_sha256) = 64 AND page_sha256 NOT GLOB '*[^0-9a-f]*'),
            rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
            PRIMARY KEY(operation_id, page_number)
        )""",
    ),
    (
        "corrupt_package_supersessions",
        """CREATE TABLE corrupt_package_supersessions (
            package_key TEXT NOT NULL PRIMARY KEY CHECK (
                length(package_key) = 64 AND package_key NOT GLOB '*[^0-9a-f]*'
            ),
            operation_id TEXT NOT NULL UNIQUE REFERENCES corrupt_package_supersession_operations(operation_id),
            observed_file_count INTEGER NOT NULL CHECK (observed_file_count >= 0),
            observed_root TEXT NOT NULL CHECK (length(observed_root) = 64 AND observed_root NOT GLOB '*[^0-9a-f]*'),
            record_path TEXT NOT NULL CHECK (length(CAST(record_path AS BLOB)) BETWEEN 1 AND 4096),
            record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64 AND record_sha256 NOT GLOB '*[^0-9a-f]*'),
            disposed_at TEXT NOT NULL
        )""",
    ),
    (
        "corrupt_purge_operations",
        """CREATE TABLE corrupt_purge_operations (
            operation_id TEXT NOT NULL PRIMARY KEY CHECK (
                length(CAST(operation_id AS BLOB)) BETWEEN 1 AND 4096
            ),
            task_id TEXT NOT NULL UNIQUE CHECK (length(CAST(task_id AS BLOB)) BETWEEN 1 AND 256),
            purge_token TEXT NOT NULL UNIQUE CHECK (length(CAST(purge_token AS BLOB)) BETWEEN 1 AND 256),
            expected_generation INTEGER NOT NULL CHECK (expected_generation >= 0),
            cursor_task_id TEXT NOT NULL DEFAULT '' CHECK (length(CAST(cursor_task_id AS BLOB)) <= 256),
            page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
            rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
            state TEXT NOT NULL CHECK (state IN ('purging','receipt-published')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    ),
    (
        "corrupt_purge_pages",
        """CREATE TABLE corrupt_purge_pages (
            operation_id TEXT NOT NULL REFERENCES corrupt_purge_operations(operation_id),
            page_number INTEGER NOT NULL CHECK (page_number >= 1),
            first_task_id TEXT NOT NULL CHECK (length(CAST(first_task_id AS BLOB)) BETWEEN 1 AND 256),
            last_task_id TEXT NOT NULL CHECK (length(CAST(last_task_id AS BLOB)) BETWEEN 1 AND 256),
            deleted_link_count INTEGER NOT NULL CHECK (deleted_link_count BETWEEN 1 AND 1000),
            page_sha256 TEXT NOT NULL CHECK (length(page_sha256) = 64 AND page_sha256 NOT GLOB '*[^0-9a-f]*'),
            rolling_root TEXT NOT NULL CHECK (length(rolling_root) = 64 AND rolling_root NOT GLOB '*[^0-9a-f]*'),
            expected_generation INTEGER NOT NULL CHECK (expected_generation >= 0),
            PRIMARY KEY(operation_id, page_number)
        )""",
    ),
)

_QUEUE_V3_INDEX_SQL = (
    (
        "queue_dedupe_identity",
        "CREATE UNIQUE INDEX queue_dedupe_identity ON tasks(dedupe_key) WHERE dedupe_key IS NOT NULL",
    ),
    (
        "queue_claim_order",
        "CREATE INDEX queue_claim_order ON tasks(state, priority DESC, available_at, created_at, id)",
    ),
    (
        "queue_redrive_parent",
        "CREATE INDEX queue_redrive_parent ON tasks(redrive_of, id)",
    ),
    (
        "queue_attempt_history",
        "CREATE INDEX queue_attempt_history ON attempt_history(task_id, sequence)",
    ),
    (
        "queue_source_tasks",
        "CREATE INDEX queue_source_tasks ON task_source_links(logical_path, source_digest, task_id)",
    ),
)

_QUEUE_V3_TRIGGER_SQL = (
    (
        "attempt_history_immutable_update",
        """CREATE TRIGGER attempt_history_immutable_update
        BEFORE UPDATE ON attempt_history
        BEGIN SELECT RAISE(ABORT, 'attempt history is immutable'); END""",
    ),
    (
        "attempt_history_authorized_delete",
        """CREATE TRIGGER attempt_history_authorized_delete
        BEFORE DELETE ON attempt_history
        WHEN NOT EXISTS (
            SELECT 1
            FROM task_purge_authorizations AS authorization
            LEFT JOIN corrupt_purge_operations AS operation
              ON operation.operation_id=authorization.operation_id
            LEFT JOIN tasks AS authorized_task
              ON authorized_task.id=authorization.task_id
            WHERE authorization.task_id=OLD.task_id AND (
                authorization.mode='ordinary'
                OR (
                    authorization.mode='corrupt-lineage'
                    AND operation.state='purging'
                    AND authorized_task.redrive_of=operation.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
                OR (
                    authorization.mode='corrupt-parent'
                    AND operation.state='receipt-published'
                    AND operation.task_id=authorization.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'attempt history delete is unauthorized'); END""",
    ),
    (
        "tasks_authorized_delete",
        """CREATE TRIGGER tasks_authorized_delete
        BEFORE DELETE ON tasks
        WHEN NOT EXISTS (
            SELECT 1
            FROM task_purge_authorizations AS authorization
            LEFT JOIN corrupt_purge_operations AS operation
              ON operation.operation_id=authorization.operation_id
            WHERE authorization.task_id=OLD.id AND (
                authorization.mode='ordinary'
                OR (
                    authorization.mode='corrupt-lineage'
                    AND operation.state='purging'
                    AND OLD.redrive_of=operation.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
                OR (
                    authorization.mode='corrupt-parent'
                    AND operation.state='receipt-published'
                    AND operation.task_id=OLD.id
                    AND authorization.authorization_digest=operation.purge_token
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'task delete is unauthorized'); END""",
    ),
    (
        "attempt_history_bounded_insert",
        """CREATE TRIGGER attempt_history_bounded_insert
        BEFORE INSERT ON attempt_history
        WHEN (SELECT COUNT(*) FROM attempt_history WHERE task_id=NEW.task_id) >= 100
        BEGIN SELECT RAISE(ABORT, 'attempt history limit exceeded'); END""",
    ),
    (
        "capture_task_links_immutable_update",
        """CREATE TRIGGER capture_task_links_immutable_update
        BEFORE UPDATE ON capture_task_links
        BEGIN SELECT RAISE(ABORT, 'capture task links are immutable'); END""",
    ),
    (
        "capture_task_links_authorized_delete",
        """CREATE TRIGGER capture_task_links_authorized_delete
        BEFORE DELETE ON capture_task_links
        WHEN NOT EXISTS (
            SELECT 1
            FROM task_purge_authorizations AS authorization
            LEFT JOIN corrupt_purge_operations AS operation
              ON operation.operation_id=authorization.operation_id
            LEFT JOIN tasks AS authorized_task
              ON authorized_task.id=authorization.task_id
            WHERE authorization.task_id=OLD.task_id AND (
                authorization.mode='ordinary'
                OR (
                    authorization.mode='corrupt-lineage'
                    AND operation.state='purging'
                    AND authorized_task.redrive_of=operation.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
                OR (
                    authorization.mode='corrupt-parent'
                    AND operation.state='receipt-published'
                    AND operation.task_id=authorization.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'capture task link delete is unauthorized'); END""",
    ),
    (
        "capture_task_link_resolutions_immutable_update",
        """CREATE TRIGGER capture_task_link_resolutions_immutable_update
        BEFORE UPDATE ON capture_task_link_resolutions
        BEGIN SELECT RAISE(ABORT, 'capture task link resolutions are immutable'); END""",
    ),
    (
        "capture_task_link_resolutions_authorized_delete",
        """CREATE TRIGGER capture_task_link_resolutions_authorized_delete
        BEFORE DELETE ON capture_task_link_resolutions
        WHEN NOT EXISTS (
            SELECT 1
            FROM task_purge_authorizations AS authorization
            LEFT JOIN corrupt_purge_operations AS operation
              ON operation.operation_id=authorization.operation_id
            LEFT JOIN tasks AS authorized_task
              ON authorized_task.id=authorization.task_id
            WHERE authorization.task_id=OLD.task_id AND (
                authorization.mode='ordinary'
                OR (
                    authorization.mode='corrupt-lineage'
                    AND operation.state='purging'
                    AND authorized_task.redrive_of=operation.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
                OR (
                    authorization.mode='corrupt-parent'
                    AND operation.state='receipt-published'
                    AND operation.task_id=authorization.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'capture task link resolution delete is unauthorized'); END""",
    ),
    (
        "capture_task_link_seals_immutable_update",
        """CREATE TRIGGER capture_task_link_seals_immutable_update
        BEFORE UPDATE ON capture_task_link_seals
        BEGIN SELECT RAISE(ABORT, 'capture task link seals are immutable'); END""",
    ),
    (
        "capture_task_link_seals_authorized_delete",
        """CREATE TRIGGER capture_task_link_seals_authorized_delete
        BEFORE DELETE ON capture_task_link_seals
        WHEN NOT EXISTS (
            SELECT 1
            FROM task_purge_authorizations AS authorization
            LEFT JOIN corrupt_purge_operations AS operation
              ON operation.operation_id=authorization.operation_id
            LEFT JOIN tasks AS authorized_task
              ON authorized_task.id=authorization.task_id
            WHERE authorization.task_id=OLD.task_id AND (
                authorization.mode='ordinary'
                OR (
                    authorization.mode='corrupt-lineage'
                    AND operation.state='purging'
                    AND authorized_task.redrive_of=operation.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
                OR (
                    authorization.mode='corrupt-parent'
                    AND operation.state='receipt-published'
                    AND operation.task_id=authorization.task_id
                    AND authorization.authorization_digest=operation.purge_token
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'capture task link seal delete is unauthorized'); END""",
    ),
    (
        "semantic_decisions_immutable_update",
        """CREATE TRIGGER semantic_decisions_immutable_update
        BEFORE UPDATE ON semantic_decisions
        BEGIN SELECT RAISE(ABORT, 'semantic decisions are immutable'); END""",
    ),
    (
        "semantic_decisions_authorized_delete",
        """CREATE TRIGGER semantic_decisions_authorized_delete
        BEFORE DELETE ON semantic_decisions
        WHEN NOT EXISTS (
            SELECT 1
            FROM capture_task_links AS link
            JOIN task_purge_authorizations AS authorization
              ON authorization.task_id=link.task_id
            WHERE authorization.mode='ordinary' AND (
                (
                    link.intent_id=OLD.intent_id
                    AND link.link_digest=OLD.active_link_digest
                )
                OR EXISTS (
                    SELECT 1 FROM capture_task_link_resolutions AS resolution
                    WHERE resolution.task_id=link.task_id
                      AND resolution.selected_intent_id=OLD.intent_id
                      AND resolution.resolution_digest=OLD.active_link_digest
                )
            )
        )
        BEGIN SELECT RAISE(ABORT, 'semantic decision delete is unauthorized'); END""",
    ),
    (
        "queue_lineage_guard_insert",
        """CREATE TRIGGER queue_lineage_guard_insert
        BEFORE INSERT ON tasks
        WHEN NEW.redrive_of IS NOT NULL AND EXISTS (
            SELECT 1 FROM tasks parent
            WHERE parent.id=NEW.redrive_of
              AND parent.state IN ('quarantine_pending','quarantined','purge_pending')
        )
        BEGIN SELECT RAISE(ABORT, 'redrive lineage is frozen'); END""",
    ),
    (
        "queue_lineage_insert",
        """CREATE TRIGGER queue_lineage_insert
        AFTER INSERT ON tasks
        WHEN NEW.redrive_of IS NOT NULL
        BEGIN
            UPDATE tasks SET lineage_generation=lineage_generation+1
            WHERE id=NEW.redrive_of;
        END""",
    ),
    (
        "queue_lineage_guard_update",
        """CREATE TRIGGER queue_lineage_guard_update
        BEFORE UPDATE OF redrive_of ON tasks
        WHEN OLD.redrive_of IS NOT NEW.redrive_of AND (
            EXISTS (
                SELECT 1 FROM tasks parent
                WHERE parent.id=OLD.redrive_of
                  AND parent.state IN ('quarantine_pending','quarantined','purge_pending')
            ) OR EXISTS (
                SELECT 1 FROM tasks parent
                WHERE parent.id=NEW.redrive_of
                  AND parent.state IN ('quarantine_pending','quarantined','purge_pending')
            )
        )
        BEGIN SELECT RAISE(ABORT, 'redrive lineage is frozen'); END""",
    ),
    (
        "queue_lineage_update_old",
        """CREATE TRIGGER queue_lineage_update_old
        AFTER UPDATE OF redrive_of ON tasks
        WHEN OLD.redrive_of IS NOT NEW.redrive_of AND OLD.redrive_of IS NOT NULL
        BEGIN
            UPDATE tasks SET lineage_generation=lineage_generation+1
            WHERE id=OLD.redrive_of;
        END""",
    ),
    (
        "queue_lineage_update_new",
        """CREATE TRIGGER queue_lineage_update_new
        AFTER UPDATE OF redrive_of ON tasks
        WHEN OLD.redrive_of IS NOT NEW.redrive_of AND NEW.redrive_of IS NOT NULL
        BEGIN
            UPDATE tasks SET lineage_generation=lineage_generation+1
            WHERE id=NEW.redrive_of;
        END""",
    ),
    (
        "queue_lineage_guard_delete",
        """CREATE TRIGGER queue_lineage_guard_delete
        BEFORE DELETE ON tasks
        WHEN OLD.redrive_of IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM tasks parent
              WHERE parent.id=OLD.redrive_of
                AND parent.state IN ('quarantine_pending','quarantined','purge_pending')
          )
          AND NOT EXISTS (
              SELECT 1 FROM task_purge_authorizations
              JOIN corrupt_purge_operations
                ON corrupt_purge_operations.operation_id=task_purge_authorizations.operation_id
               AND corrupt_purge_operations.task_id=task_purge_authorizations.task_id
              WHERE task_purge_authorizations.task_id=OLD.redrive_of
                AND task_purge_authorizations.mode='corrupt-lineage'
                AND corrupt_purge_operations.state='purging'
                AND task_purge_authorizations.authorization_digest=
                    corrupt_purge_operations.purge_token
          )
        BEGIN SELECT RAISE(ABORT, 'redrive lineage is frozen'); END""",
    ),
    (
        "queue_lineage_delete",
        """CREATE TRIGGER queue_lineage_delete
        AFTER DELETE ON tasks
        WHEN OLD.redrive_of IS NOT NULL
        BEGIN
            UPDATE tasks SET lineage_generation=lineage_generation+1
            WHERE id=OLD.redrive_of;
        END""",
    ),
)

_QUEUE_V3_TRIGGER_SQL += tuple(
    (
        f"{table}_immutable_{operation}",
        f"""CREATE TRIGGER {table}_immutable_{operation}
        BEFORE {operation.upper()} ON {table}
        BEGIN SELECT RAISE(ABORT, '{table} rows are immutable'); END""",
    )
    for table in (
        "corrupt_export_pages",
        "corrupt_dispositions",
        "corrupt_package_supersession_pages",
        "corrupt_package_supersessions",
        "corrupt_purge_pages",
    )
    for operation in ("update", "delete")
)

_QUEUE_V3_TRIGGER_SQL += (
    (
        "corrupt_export_operations_immutable_delete",
        """CREATE TRIGGER corrupt_export_operations_immutable_delete
        BEFORE DELETE ON corrupt_export_operations
        BEGIN SELECT RAISE(ABORT, 'corrupt export operations cannot be deleted'); END""",
    ),
    (
        "corrupt_export_operations_monotonic_update",
        """CREATE TRIGGER corrupt_export_operations_monotonic_update
        BEFORE UPDATE ON corrupt_export_operations
        WHEN NOT (
            NEW.operation_id IS OLD.operation_id
            AND NEW.task_id IS OLD.task_id
            AND NEW.disposition_key IS OLD.disposition_key
            AND NEW.task_fence_token_digest IS OLD.task_fence_token_digest
            AND NEW.task_fence_epoch IS OLD.task_fence_epoch
            AND NEW.intent_fence_digest IS OLD.intent_fence_digest
            AND NEW.raw_sha256 IS OLD.raw_sha256
            AND NEW.history_sha256 IS OLD.history_sha256
            AND NEW.metadata_sha256 IS OLD.metadata_sha256
            AND NEW.lineage_generation IS OLD.lineage_generation
            AND NEW.actor_identity IS OLD.actor_identity
            AND NEW.reason IS OLD.reason
            AND NEW.created_at IS OLD.created_at
            AND NEW.cursor_task_id >= OLD.cursor_task_id
            AND NEW.link_count >= OLD.link_count
            AND NEW.page_count >= OLD.page_count
            AND NEW.updated_at >= OLD.updated_at
            AND (
                NEW.state = OLD.state
                OR (OLD.state='exporting' AND NEW.state='manifested')
                OR (OLD.state='manifested' AND NEW.state='disposed')
            )
            AND (
                NEW.rolling_root IS OLD.rolling_root
                OR NEW.cursor_task_id > OLD.cursor_task_id
                OR NEW.link_count > OLD.link_count
                OR NEW.page_count > OLD.page_count
            )
        )
        BEGIN SELECT RAISE(ABORT, 'corrupt export operation update is not monotonic'); END""",
    ),
    (
        "corrupt_package_supersession_operations_immutable_delete",
        """CREATE TRIGGER corrupt_package_supersession_operations_immutable_delete
        BEFORE DELETE ON corrupt_package_supersession_operations
        BEGIN SELECT RAISE(ABORT, 'package supersession operations cannot be deleted'); END""",
    ),
    (
        "corrupt_package_supersession_operations_monotonic_update",
        """CREATE TRIGGER corrupt_package_supersession_operations_monotonic_update
        BEFORE UPDATE ON corrupt_package_supersession_operations
        WHEN NOT (
            NEW.operation_id IS OLD.operation_id
            AND NEW.package_key IS OLD.package_key
            AND NEW.package_path IS OLD.package_path
            AND NEW.actor_identity IS OLD.actor_identity
            AND NEW.reason IS OLD.reason
            AND NEW.chosen_at IS OLD.chosen_at
            AND NEW.created_at IS OLD.created_at
            AND NEW.cursor_name >= OLD.cursor_name
            AND NEW.file_count >= OLD.file_count
            AND NEW.page_count >= OLD.page_count
            AND NEW.updated_at >= OLD.updated_at
            AND (
                NEW.state = OLD.state
                OR (OLD.state='scanning' AND NEW.state='disposed')
            )
            AND (
                NEW.rolling_root IS OLD.rolling_root
                OR NEW.cursor_name > OLD.cursor_name
                OR NEW.file_count > OLD.file_count
                OR NEW.page_count > OLD.page_count
            )
        )
        BEGIN SELECT RAISE(ABORT, 'package supersession update is not monotonic'); END""",
    ),
    (
        "corrupt_purge_operations_immutable_delete",
        """CREATE TRIGGER corrupt_purge_operations_immutable_delete
        BEFORE DELETE ON corrupt_purge_operations
        BEGIN SELECT RAISE(ABORT, 'corrupt purge operations cannot be deleted'); END""",
    ),
    (
        "corrupt_purge_operations_monotonic_update",
        """CREATE TRIGGER corrupt_purge_operations_monotonic_update
        BEFORE UPDATE ON corrupt_purge_operations
        WHEN NOT (
            NEW.operation_id IS OLD.operation_id
            AND NEW.task_id IS OLD.task_id
            AND NEW.purge_token IS OLD.purge_token
            AND NEW.expected_generation >= OLD.expected_generation
            AND NEW.created_at IS OLD.created_at
            AND NEW.cursor_task_id >= OLD.cursor_task_id
            AND NEW.page_count >= OLD.page_count
            AND NEW.updated_at >= OLD.updated_at
            AND (
                NEW.state = OLD.state
                OR (OLD.state='purging' AND NEW.state='receipt-published')
            )
            AND (
                NEW.rolling_root IS OLD.rolling_root
                OR NEW.cursor_task_id > OLD.cursor_task_id
                OR NEW.page_count > OLD.page_count
                OR NEW.expected_generation > OLD.expected_generation
                OR NEW.state != OLD.state
            )
        )
        BEGIN SELECT RAISE(ABORT, 'corrupt purge operation update is not monotonic'); END""",
    ),
)

QUEUE_V3_SCHEMA_SHA256 = sha256_bytes(
    canonical_json_bytes(
        {
            "tables": [list(item) for item in _QUEUE_V3_TABLE_SQL],
            "indexes": [list(item) for item in _QUEUE_V3_INDEX_SQL],
            "triggers": [list(item) for item in _QUEUE_V3_TRIGGER_SQL],
            "application_id": _QUEUE_V3_CONTRACT.application_id,
            "user_version": _QUEUE_V3_CONTRACT.user_version,
        }
    )
)


def _normalized_sql(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _queue_v3_object_matches(
    database: sqlite3.Connection, kind: str, name: str, sql: str
) -> bool:
    row = database.execute(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?", (kind, name)
    ).fetchone()
    return row is not None and _normalized_sql(row[0]) == _normalized_sql(sql)


def _queue_v3_statements(
    *, include_triggers: bool = True
) -> tuple[MigrationStatement, ...]:
    statements: list[MigrationStatement] = []
    definitions_by_kind = [
        ("table", _QUEUE_V3_TABLE_SQL),
        ("index", _QUEUE_V3_INDEX_SQL),
    ]
    if include_triggers:
        definitions_by_kind.append(("trigger", _QUEUE_V3_TRIGGER_SQL))
    for kind, definitions in definitions_by_kind:
        for name, sql in definitions:
            statements.append(
                MigrationStatement(
                    name=f"create_{name}",
                    sql=sql,
                    completed=lambda database, expected_kind=kind, expected_name=name, expected_sql=sql: (
                        _queue_v3_object_matches(
                            database, expected_kind, expected_name, expected_sql
                        )
                    ),
                )
            )
    return tuple(statements)


def _queue_v3_schema_complete(
    database: sqlite3.Connection, *, include_triggers: bool = True
) -> bool:
    definitions_by_kind = [
        ("table", _QUEUE_V3_TABLE_SQL),
        ("index", _QUEUE_V3_INDEX_SQL),
    ]
    if include_triggers:
        definitions_by_kind.append(("trigger", _QUEUE_V3_TRIGGER_SQL))
    return all(
        _queue_v3_object_matches(database, kind, name, sql)
        for kind, definitions in definitions_by_kind
        for name, sql in definitions
    )


def _migration_error(code: str, message: str) -> OperationalDatabaseContractError:
    error = OperationalDatabaseContractError(message)
    error.code = code
    return error


def _ordinary_purge_operation_id(manifest_sha256: str) -> str:
    return f"ordinary-purge:{manifest_sha256}"


def _capture_purge_archive_path(task_id: str, source_path: str) -> str:
    key = sha256_bytes(
        canonical_json_bytes({"source_path": source_path, "task_id": task_id})
    )
    return f"capture-artifacts/{key}.artifact"


def _ordinary_purge_result_archive_path(task_id: str) -> str:
    value = f"results/{task_id}.result"
    try:
        restricted_relative_path(value, ("results",))
    except ValueError as exc:
        raise QueueOperationError("export_verification_failed") from exc
    return value


def _ordinary_purge_authorization_digest(
    task_id: str, operation_id: str, manifest_sha256: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "manifest_sha256": manifest_sha256,
                "operation_id": operation_id,
                "task_id": task_id,
            }
        )
    )


def _ordinary_purge_manifest_digest(operation_id: object) -> str | None:
    prefix = "ordinary-purge:"
    if not isinstance(operation_id, str) or not operation_id.startswith(prefix):
        return None
    digest = operation_id.removeprefix(prefix)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None
    return digest


def _queue_v3_purge_authorization_valid(row: sqlite3.Row) -> bool:
    manifest_sha256 = _ordinary_purge_manifest_digest(row["operation_id"])
    if row["mode"] != "ordinary" or row["task_present"]:
        return False
    if manifest_sha256 is None:
        return False
    expected = _ordinary_purge_authorization_digest(
        str(row["task_id"]), str(row["operation_id"]), manifest_sha256
    )
    return row["authorization_digest"] == expected


def _queue_v3_purge_authorizations_valid(database: sqlite3.Connection) -> bool:
    rows = database.execute(
        """SELECT authorization.*,
                  EXISTS(SELECT 1 FROM tasks WHERE id=authorization.task_id)
                    AS task_present
           FROM task_purge_authorizations AS authorization"""
    ).fetchall()
    return all(_queue_v3_purge_authorization_valid(row) for row in rows)


_QUEUE_V3_DROP_ORDER = {"trigger": 0, "index": 1, "table": 2}
_QUEUE_V3_DROP_KEYWORD = {"index": "INDEX", "table": "TABLE", "trigger": "TRIGGER"}


def _expected_queue_v3_schema() -> dict[tuple[str, str], str]:
    """Every schema object a complete queue v3 candidate is made of."""
    return {
        (kind, name): sql
        for kind, definitions in (
            ("table", _QUEUE_V3_TABLE_SQL),
            ("index", _QUEUE_V3_INDEX_SQL),
            ("trigger", _QUEUE_V3_TRIGGER_SQL),
        )
        for name, sql in definitions
    }


def _queue_schema_rows(database: sqlite3.Connection) -> list[sqlite3.Row]:
    return database.execute(
        """SELECT type, name, sql FROM sqlite_schema
           WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"""
    ).fetchall()


def _drop_order_key(item: tuple[str, str]) -> tuple[int, str]:
    """Triggers before indexes before tables, so a drop never dangles."""
    return _QUEUE_V3_DROP_ORDER.get(item[0], 3), item[1]


def _drop_schema_object(
    database: sqlite3.Connection, kind: str, name: str
) -> None:
    """Drop one schema object, refusing a kind this migration never creates."""
    keyword = _QUEUE_V3_DROP_KEYWORD.get(kind)
    if keyword is None:
        raise _migration_error(
            "queue_v3_schema_conflict",
            "queue v3 candidate has an unsupported schema object",
        )
    with begin_immediate(database):
        database.execute(f'DROP {keyword} "{name}"')


def _drop_unexpected_objects(
    database: sqlite3.Connection,
    unexpected: list[tuple[str, str]],
    *,
    allow_populated_rebuild: bool,
) -> None:
    """Objects we never create must go before the candidate can be trusted."""
    if not allow_populated_rebuild:
        raise _migration_error(
            "queue_v3_schema_conflict",
            "queue v3 candidate has unexpected schema objects",
        )
    for kind, name in sorted(unexpected, key=_drop_order_key):
        _drop_schema_object(database, kind, name)


def _table_is_populated(database: sqlite3.Connection, name: str) -> bool:
    """An unreadable table counts as populated; we must not rebuild it blind."""
    try:
        row = database.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone()
    except sqlite3.DatabaseError:
        return True
    return row is not None


def _check_no_populated_rebuild(
    database: sqlite3.Connection,
    malformed: list[tuple[str, str]],
    *,
    allow_populated_rebuild: bool,
) -> None:
    """Refuse to rebuild a partial table that still holds rows."""
    if allow_populated_rebuild:
        return
    for kind, name in malformed:
        if kind == "table" and _table_is_populated(database, name):
            raise _migration_error(
                "queue_v3_schema_conflict",
                "queue v3 candidate has a populated partial table",
            )


def _malformed_queue_v3_objects(
    database: sqlite3.Connection,
    existing: list[sqlite3.Row],
    expected: Mapping[tuple[str, str], str],
) -> list[tuple[str, str]]:
    """Objects whose definition differs from the one this version creates."""
    return [
        (str(row["type"]), str(row["name"]))
        for row in existing
        if not _queue_v3_object_matches(
            database,
            str(row["type"]),
            str(row["name"]),
            expected[(str(row["type"]), str(row["name"]))],
        )
    ]


def _repair_partial_queue_v3_schema(
    database: sqlite3.Connection, *, allow_populated_rebuild: bool
) -> None:
    expected = _expected_queue_v3_schema()
    existing = _queue_schema_rows(database)
    unexpected = [
        (str(row["type"]), str(row["name"]))
        for row in existing
        if (str(row["type"]), str(row["name"])) not in expected
    ]
    if unexpected:
        _drop_unexpected_objects(
            database, unexpected, allow_populated_rebuild=allow_populated_rebuild
        )
        existing = _queue_schema_rows(database)
    malformed = _malformed_queue_v3_objects(database, existing, expected)
    _check_no_populated_rebuild(
        database, malformed, allow_populated_rebuild=allow_populated_rebuild
    )
    for kind, name in sorted(malformed, key=_drop_order_key):
        _drop_schema_object(database, kind, name)


def _queue_v2_tables(database: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in database.execute(
            "SELECT name FROM sqlite_schema WHERE type='table'"
        ).fetchall()
    }


_QUEUE_V2_SCHEMA_OBJECTS = {
    ("index", "queue_claim_order"),
    ("table", "attempt_history"),
    ("table", "queue_ownership"),
    ("table", "source_failures"),
    ("table", "source_fences"),
    ("table", "tasks"),
    ("trigger", "attempt_history_immutable_delete"),
    ("trigger", "attempt_history_immutable_update"),
}


def _validate_queue_v2_schema_objects(database: sqlite3.Connection) -> None:
    objects = {
        (str(row[0]), str(row[1]))
        for row in database.execute(
            """SELECT type, name FROM sqlite_schema
               WHERE name NOT LIKE 'sqlite_%'"""
        )
    }
    if not objects <= _QUEUE_V2_SCHEMA_OBJECTS:
        raise _migration_error(
            "queue_v2_schema_unknown",
            "queue v2 source has unknown schema objects",
        )


def _queue_v2_columns(database: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in database.execute(f'PRAGMA table_info("{table}")')}


def _queue_v3_source_links(payload_bytes: bytes) -> tuple[tuple[str, str], ...]:
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ()
    paths: set[str] = set()
    digests: set[str] = set()
    daily_ids: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = str(key).casefold()
                if isinstance(item, str):
                    if normalized in {"source_path", "logical_path"}:
                        paths.add(item)
                    elif normalized in {"source_digest", "digest", "hash"}:
                        digests.add(item)
                    elif normalized == "daily_id":
                        daily_ids.add(item)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    if not paths and len(daily_ids) == 1:
        paths.add(f"knowledge/daily/{next(iter(daily_ids))}.md")
    if not paths and not digests:
        return ()
    if len(paths) != 1 or len(digests) != 1:
        raise _migration_error(
            "queue_v2_source_identity_ambiguous",
            "queue v2 payload has an ambiguous source identity",
        )
    logical_path = next(iter(paths))
    source_digest = next(iter(digests))
    try:
        MemoryQueue._validate_failure_identity(logical_path, source_digest)
    except ValueError as exc:
        raise _migration_error(
            "queue_v2_source_identity_ambiguous",
            "queue v2 payload has an invalid source identity",
        ) from exc
    return ((logical_path, source_digest),)


def _queue_v3_row_matches(
    database: sqlite3.Connection,
    table: str,
    key_column: str,
    key: object,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> bool:
    selected = ", ".join(f'"{column}"' for column in columns)
    row = database.execute(
        f'SELECT {selected} FROM "{table}" WHERE "{key_column}"=?', (key,)
    ).fetchone()
    return row is not None and tuple(row) == values


def _queue_v3_insert_statement(
    *,
    name: str,
    table: str,
    columns: tuple[str, ...],
    values: tuple[object, ...],
    key_column: str,
    key: object,
) -> MigrationStatement:
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    return MigrationStatement(
        name=name,
        sql=f'INSERT OR IGNORE INTO "{table}" ({quoted}) VALUES ({placeholders})',
        parameters=values,
        completed=lambda database: _queue_v3_row_matches(
            database, table, key_column, key, columns, values
        ),
    )


_QUEUE_V2_REQUIRED_TASK_COLUMNS = {
    "id",
    "kind",
    "handler_version",
    "payload_json",
    "input_hash",
    "dedupe_key",
    "state",
    "priority",
    "created_at",
    "updated_at",
    "available_at",
    "attempts",
    "last_attempt_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "lease_heartbeat_at",
    "attempt_started_at",
    "error_code",
    "blocked_capability",
    "result_reference",
    "result_sha256",
    "result_operation_id",
    "redrive_of",
}
_QUEUE_V3_TASK_COLUMNS = (
    "id",
    "kind",
    "handler_version",
    "payload_blob",
    "input_hash",
    "dedupe_key",
    "state",
    "priority",
    "created_at",
    "updated_at",
    "available_at",
    "attempts",
    "last_attempt_at",
    "lease_owner",
    "lease_token",
    "lease_expires_at",
    "lease_heartbeat_at",
    "attempt_started_at",
    "error_code",
    "blocked_capability",
    "result_reference",
    "result_sha256",
    "result_operation_id",
    "redrive_of",
    "lineage_generation",
)
_QUEUE_V2_HISTORY_COLUMNS = (
    "sequence",
    "task_id",
    "attempt",
    "started_at",
    "finished_at",
    "outcome",
    "error_code",
)
_QUEUE_V2_FAILURE_COLUMNS = (
    "logical_path",
    "source_digest",
    "error_code",
    "producer",
    "updated_at",
)


def _require_queue_v2_tasks_schema(
    source: sqlite3.Connection, tables: set[str]
) -> None:
    if "tasks" not in tables:
        raise _migration_error(
            "queue_v2_schema_incomplete", "queue v2 source has no tasks table"
        )
    if not _QUEUE_V2_REQUIRED_TASK_COLUMNS <= _queue_v2_columns(source, "tasks"):
        raise _migration_error(
            "queue_v2_schema_incomplete", "queue v2 tasks schema is incomplete"
        )


def _require_unambiguous_v2_owners(
    source: sqlite3.Connection, tables: set[str]
) -> None:
    """v2 fences and owners predate the identity v3 needs to fence with."""
    if "source_fences" in tables and source.execute(
        "SELECT 1 FROM source_fences LIMIT 1"
    ).fetchone() is not None:
        raise _migration_error(
            "queue_v2_source_fence_ambiguous",
            "queue v2 source fences lack process-start identity",
        )
    if "queue_ownership" in tables and source.execute(
        "SELECT 1 FROM queue_ownership WHERE token IS NOT NULL LIMIT 1"
    ).fetchone() is not None:
        raise _migration_error(
            "queue_v2_owner_ambiguous",
            "queue v2 ownership lacks canonical process identity",
        )


def _queue_v2_child_counts(
    task_rows: list, rows_by_id: dict[str, sqlite3.Row]
) -> dict[str, int]:
    child_counts = {task_id: 0 for task_id in rows_by_id}
    for row in task_rows:
        parent = row["redrive_of"]
        if parent is None:
            continue
        if str(parent) not in rows_by_id:
            raise _migration_error(
                "queue_v2_lineage_ambiguous", "queue v2 redrive parent is missing"
            )
        child_counts[str(parent)] += 1
    return child_counts


def _parents_before_children(rows_by_id: dict[str, sqlite3.Row]) -> list:
    """Redrive parents must be inserted before the tasks that name them."""
    ordered_rows: list[sqlite3.Row] = []
    visited: set[str] = set()
    visiting: set[str] = set()

    def visit_task(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise _migration_error(
                "queue_v2_lineage_ambiguous", "queue v2 redrive lineage is cyclic"
            )
        visiting.add(task_id)
        parent = rows_by_id[task_id]["redrive_of"]
        if parent is not None:
            visit_task(str(parent))
        visiting.remove(task_id)
        visited.add(task_id)
        ordered_rows.append(rows_by_id[task_id])

    for task_id in sorted(rows_by_id):
        visit_task(task_id)
    return ordered_rows


def _ordered_queue_v2_tasks(source: sqlite3.Connection) -> tuple[list, dict[str, int]]:
    task_rows = list(source.execute("SELECT * FROM tasks ORDER BY id"))
    rows_by_id = {str(row["id"]): row for row in task_rows}
    if len(rows_by_id) != len(task_rows):
        raise _migration_error(
            "queue_v2_task_identity_conflict", "queue v2 task IDs are not unique"
        )
    child_counts = _queue_v2_child_counts(task_rows, rows_by_id)
    return _parents_before_children(rows_by_id), child_counts


def _migrated_payload_bytes(row: sqlite3.Row) -> bytes:
    payload_text = row["payload_json"]
    if not isinstance(payload_text, str):
        raise _migration_error(
            "queue_v2_payload_not_text", "queue v2 payload is not TEXT"
        )
    try:
        payload_bytes = payload_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _migration_error(
            "queue_v2_payload_not_utf8", "queue v2 payload is not UTF-8"
        ) from exc
    if len(payload_bytes) > _MAX_QUEUE_PAYLOAD_BYTES:
        raise _migration_error(
            "queue_v2_payload_too_large", "queue v2 payload exceeds 1 MiB"
        )
    return payload_bytes


def _migrated_attempts(row: sqlite3.Row) -> int:
    attempts = row["attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise _migration_error(
            "queue_v2_attempts_out_of_range", "queue v2 attempts exceed v3 bounds"
        )
    if not 0 <= attempts <= 100:
        raise _migration_error(
            "queue_v2_attempts_out_of_range", "queue v2 attempts exceed v3 bounds"
        )
    return attempts


def _migrated_task_values(
    row: sqlite3.Row,
    payload_bytes: bytes,
    attempts: int,
    child_counts: dict[str, int],
    mismatch: bool,
) -> tuple:
    state = row["state"]
    error_code = row["error_code"]
    if mismatch:
        state = "dead"
        error_code = "payload_hash_mismatch"
    return (
        row["id"],
        row["kind"],
        row["handler_version"],
        payload_bytes,
        row["input_hash"],
        row["dedupe_key"],
        state,
        row["priority"],
        row["created_at"],
        row["updated_at"],
        row["available_at"],
        attempts,
        row["last_attempt_at"],
        row["lease_owner"],
        row["lease_token"],
        row["lease_expires_at"],
        row["lease_heartbeat_at"],
        row["attempt_started_at"],
        error_code,
        row["blocked_capability"],
        row["result_reference"],
        row["result_sha256"],
        row["result_operation_id"],
        row["redrive_of"],
        child_counts[str(row["id"])],
    )


def _task_migration_statements(
    ordered_rows: list,
    child_counts: dict[str, int],
    statements: list[MigrationStatement],
    summary: dict[str, int],
) -> list[tuple[str, str, str]]:
    """Task inserts in lineage order; returns the source links they imply."""
    task_links: list[tuple[str, str, str]] = []
    for row in ordered_rows:
        payload_bytes = _migrated_payload_bytes(row)
        attempts = _migrated_attempts(row)
        mismatch = (
            validate_payload_blob(
                payload_bytes, row["input_hash"], parse=True
            ).code
            is not None
        )
        task_id = str(row["id"])
        statements.append(
            _queue_v3_insert_statement(
                name=f"migrate_task_{sha256_bytes(task_id.encode('utf-8'))[:16]}",
                table="tasks",
                columns=_QUEUE_V3_TASK_COLUMNS,
                values=_migrated_task_values(
                    row, payload_bytes, attempts, child_counts, mismatch
                ),
                key_column="id",
                key=row["id"],
            )
        )
        summary["tasks"] += 1
        if mismatch:
            summary["payload_hash_mismatches"] += 1
            continue
        task_links.extend(
            (task_id, logical_path, source_digest)
            for logical_path, source_digest in _queue_v3_source_links(payload_bytes)
        )
    return task_links


def _attempt_history_statements(
    source: sqlite3.Connection,
    tables: set[str],
    statements: list[MigrationStatement],
    summary: dict[str, int],
) -> None:
    if "attempt_history" not in tables:
        return
    if set(_QUEUE_V2_HISTORY_COLUMNS) != _queue_v2_columns(source, "attempt_history"):
        raise _migration_error(
            "queue_v2_schema_incomplete",
            "queue v2 attempt history schema is incomplete",
        )
    for row in source.execute("SELECT * FROM attempt_history ORDER BY sequence"):
        statements.append(
            _queue_v3_insert_statement(
                name=f"migrate_attempt_{int(row['sequence'])}",
                table="attempt_history",
                columns=_QUEUE_V2_HISTORY_COLUMNS,
                values=tuple(row[column] for column in _QUEUE_V2_HISTORY_COLUMNS),
                key_column="sequence",
                key=row["sequence"],
            )
        )
        summary["attempt_history"] += 1


def _task_source_link_statements(
    task_links: list[tuple[str, str, str]],
    statements: list[MigrationStatement],
    summary: dict[str, int],
) -> None:
    for task_id, logical_path, source_digest in sorted(task_links):
        values = (task_id, logical_path, source_digest)
        identity = sha256_bytes(canonical_json_bytes(list(values)))[:16]
        statements.append(
            MigrationStatement(
                name=f"migrate_task_source_link_{identity}",
                sql="""INSERT OR IGNORE INTO task_source_links(
                    task_id, logical_path, source_digest
                ) VALUES (?, ?, ?)""",
                parameters=values,
                completed=lambda database, expected=values: database.execute(
                    """SELECT 1 FROM task_source_links
                       WHERE task_id=? AND logical_path=? AND source_digest=?""",
                    expected,
                ).fetchone()
                is not None,
            )
        )
        summary["task_source_links"] += 1


def _source_failure_statements(
    source: sqlite3.Connection,
    tables: set[str],
    statements: list[MigrationStatement],
    summary: dict[str, int],
) -> None:
    if "source_failures" not in tables:
        return
    if set(_QUEUE_V2_FAILURE_COLUMNS) != _queue_v2_columns(source, "source_failures"):
        raise _migration_error(
            "queue_v2_schema_incomplete",
            "queue v2 source failure schema is incomplete",
        )
    for row in source.execute(
        "SELECT * FROM source_failures ORDER BY logical_path, source_digest"
    ):
        values = tuple(row[column] for column in _QUEUE_V2_FAILURE_COLUMNS)
        identity = sha256_bytes(canonical_json_bytes(list(values[:2])))[:16]
        statements.append(
            MigrationStatement(
                name=f"migrate_source_failure_{identity}",
                sql="""INSERT OR IGNORE INTO source_failures(
                    logical_path, source_digest, error_code, producer, updated_at
                ) VALUES (?, ?, ?, ?, ?)""",
                parameters=values,
                completed=lambda database, expected=values: tuple(
                    database.execute(
                        """SELECT error_code, producer, updated_at FROM source_failures
                           WHERE logical_path=? AND source_digest=?""",
                        expected[:2],
                    ).fetchone()
                    or ()
                )
                == tuple(expected[2:]),
            )
        )
        summary["source_failures"] += 1


def _queue_v2_migration_statements(
    source: sqlite3.Connection,
) -> tuple[tuple[MigrationStatement, ...], dict[str, int]]:
    _validate_queue_v2_schema_objects(source)
    tables = _queue_v2_tables(source)
    _require_queue_v2_tasks_schema(source, tables)
    _require_unambiguous_v2_owners(source, tables)
    statements: list[MigrationStatement] = []
    summary = {
        "attempt_history": 0,
        "payload_hash_mismatches": 0,
        "source_failures": 0,
        "source_fences": 0,
        "task_source_links": 0,
        "tasks": 0,
    }
    ordered_rows, child_counts = _ordered_queue_v2_tasks(source)
    task_links = _task_migration_statements(
        ordered_rows, child_counts, statements, summary
    )
    _attempt_history_statements(source, tables, statements, summary)
    _task_source_link_statements(task_links, statements, summary)
    _source_failure_statements(source, tables, statements, summary)
    return tuple(statements), summary


def _queue_v2_reconciliation_complete(
    database: sqlite3.Connection,
    statements: tuple[MigrationStatement, ...],
    summary: Mapping[str, int],
) -> bool:
    if not all(statement.completed(database) for statement in statements):
        return False
    expected = {
        "attempt_history": summary["attempt_history"],
        "source_failures": summary["source_failures"],
        "source_fences": summary["source_fences"],
        "task_source_links": summary["task_source_links"],
        "tasks": summary["tasks"],
    }
    return all(
        database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == count
        for table, count in expected.items()
    ) and all(
        database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
        for table in (
            "capture_intents",
            "capture_task_link_resolutions",
            "capture_task_link_seals",
            "capture_task_links",
            "corrupt_dispositions",
            "corrupt_export_operations",
            "corrupt_export_pages",
            "corrupt_package_supersession_operations",
            "corrupt_package_supersession_pages",
            "corrupt_package_supersessions",
            "corrupt_purge_operations",
            "corrupt_purge_pages",
            "queue_ownership",
            "semantic_decisions",
            "task_fence_epochs",
            "task_fences",
            "task_purge_authorizations",
        )
    )


def _queue_v3_row_counts(database: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table, _sql in _QUEUE_V3_TABLE_SQL
    }


def _reject_queue_source_alias(candidate: Path, source: Path) -> None:
    if candidate.absolute() == source.absolute():
        raise ValueError("queue v3 candidate and v2 source are the same file")
    try:
        candidate_present = candidate.exists() or candidate.is_symlink()
        if candidate_present and os.path.samefile(candidate, source):
            raise ValueError("queue v3 candidate and v2 source are the same file")
    except ValueError:
        raise
    except OSError as exc:
        raise PermissionError("queue v3 candidate identity could not be verified") from exc


def _queue_v3_payloads_valid(database: sqlite3.Connection) -> bool:
    for row in database.execute(
        "SELECT payload_blob, input_hash, state, error_code FROM tasks"
    ):
        payload_bytes = bytes(row["payload_blob"])
        validation = validate_payload_blob(
            payload_bytes, row["input_hash"], parse=True
        )
        if validation.code is not None and (
            row["state"] != "dead" or row["error_code"] != "payload_hash_mismatch"
        ):
            return False
    return True


_EMPTY_QUEUE_V3_SUMMARY = MappingProxyType(
    {
        "attempt_history": 0,
        "payload_hash_mismatches": 0,
        "source_failures": 0,
        "source_fences": 0,
        "task_source_links": 0,
        "tasks": 0,
    }
)


def _open_queue_v2_source(
    stack: ExitStack, candidate: Path, source_path: Path
) -> sqlite3.Connection:
    """Open the v2 source read-only, refusing one that aliases the candidate."""
    _reject_queue_source_alias(candidate, source_path)
    source = stack.enter_context(
        closing(
            open_readonly_operational_db(
                source_path,
                source_path.parent.parent,
                max_bytes=1 << 50,
                owner_only=True,
            )
        )
    )
    source.row_factory = sqlite3.Row
    source.execute("BEGIN")
    return source


def _queue_v2_migration_plan(
    stack: ExitStack, candidate: Path, source_path: Path | None
) -> tuple[tuple[MigrationStatement, ...], dict[str, int]]:
    """What the v2 source contributes, or nothing when there is no source."""
    if source_path is None:
        return (), dict(_EMPTY_QUEUE_V3_SUMMARY)
    source = _open_queue_v2_source(stack, candidate, source_path)
    statements, summary = _queue_v2_migration_statements(source)
    _reject_queue_source_alias(candidate, source_path)
    return statements, summary


def _queue_v3_trigger_statements() -> tuple[MigrationStatement, ...]:
    """Only the statements that create the v3 triggers."""
    trigger_names = {name for name, _sql in _QUEUE_V3_TRIGGER_SQL}
    return tuple(
        statement
        for statement in _queue_v3_statements()
        if statement.name.startswith("create_")
        and statement.name.removeprefix("create_") in trigger_names
    )


def _require_empty_queue_v3(database: sqlite3.Connection) -> None:
    """A fresh initialization may not find rows already there."""
    if any(_queue_v3_row_counts(database).values()):
        raise _migration_error(
            "queue_v3_source_conflict",
            "fresh queue v3 initialization found existing rows",
        )


def _validate_queue_v3_candidate(database: sqlite3.Connection) -> None:
    """The candidate has to be internally consistent before it is published."""
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        raise _migration_error(
            "queue_v3_validation_failed", "queue v3 candidate validation failed"
        )
    if database.execute("PRAGMA foreign_key_check").fetchall():
        raise _migration_error(
            "queue_v3_validation_failed", "queue v3 candidate validation failed"
        )
    if not _queue_v3_payloads_valid(database):
        raise _migration_error(
            "queue_v3_validation_failed", "queue v3 candidate validation failed"
        )


def _build_queue_v3_candidate(
    stack: ExitStack,
    candidate: Path,
    source_path: Path | None,
    killpoint: Callable[[str], None] | None,
) -> dict[str, int]:
    """Create or resume the candidate's schema and content, then validate it."""
    migration_statements, summary = _queue_v2_migration_plan(
        stack, candidate, source_path
    )
    database = stack.enter_context(
        closing(open_operational_db(candidate, busy_ms=DEFAULTS.queue_busy_ms))
    )
    if source_path is not None:
        _reject_queue_source_alias(candidate, source_path)
    _repair_partial_queue_v3_schema(
        database, allow_populated_rebuild=source_path is not None
    )
    run_resumable_migration(
        database,
        _queue_v3_statements(include_triggers=False),
        final_invariant=lambda current: _queue_v3_schema_complete(
            current, include_triggers=False
        ),
        killpoint=killpoint,
    )
    _apply_queue_v2_content(
        database, migration_statements, summary, source_path, killpoint
    )
    run_resumable_migration(
        database,
        _queue_v3_trigger_statements(),
        final_invariant=_queue_v3_schema_complete,
        killpoint=killpoint,
    )
    _validate_queue_v3_candidate(database)
    return summary


def _apply_queue_v2_content(
    database: sqlite3.Connection,
    migration_statements: tuple[MigrationStatement, ...],
    summary: Mapping[str, int],
    source_path: Path | None,
    killpoint: Callable[[str], None] | None,
) -> None:
    """Copy the v2 rows in, or prove the fresh candidate has none."""
    if source_path is None:
        _require_empty_queue_v3(database)
        return
    run_resumable_migration(
        database,
        migration_statements,
        final_invariant=lambda current: _queue_v2_reconciliation_complete(
            current, migration_statements, summary
        ),
        killpoint=killpoint,
    )


def _stamp_queue_v3_contract(candidate: Path) -> None:
    """Write the contract row that marks the candidate as a v3 database."""
    with closing(
        open_operational_db(
            candidate,
            busy_ms=DEFAULTS.queue_busy_ms,
            contract=_QUEUE_V3_CONTRACT,
            initialize_contract=True,
        )
    ):
        pass


def initialize_queue_v3_candidate(
    path: Path,
    *,
    source_v2: Path | None,
    killpoint: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Create or resume an unpublished queue v3 candidate database."""
    candidate = Path(path)
    source_path = Path(source_v2) if source_v2 is not None else None
    with ExitStack() as stack:
        summary = _build_queue_v3_candidate(
            stack, candidate, source_path, killpoint
        )
    _stamp_queue_v3_contract(candidate)
    _harden_owner_only(candidate, 0o600)
    validate_queue_v3_database(candidate, state_root=candidate.parent.parent)
    return summary


def validate_queue_v3_database(
    path: Path, *, state_root: Path
) -> dict[str, object]:
    """Validate one unpublished or active queue v3 database fail-closed."""
    try:
        Path(path).resolve(strict=True).relative_to(Path(state_root).resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PermissionError("queue v3 database is outside the state root") from exc
    with closing(
        open_readonly_operational_db(
            Path(path),
            Path(state_root),
            max_bytes=1 << 50,
            owner_only=True,
            busy_ms=DEFAULTS.queue_busy_ms,
            contract=_QUEUE_V3_CONTRACT,
        )
    ) as database:
        if not _queue_v3_schema_complete(database):
            raise _migration_error(
                "queue_v3_schema_incomplete", "queue v3 schema is incomplete"
            )
        integrity = database.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = database.execute("PRAGMA foreign_key_check").fetchall()
        payload_violations = database.execute(
            """SELECT COUNT(*) FROM tasks
               WHERE typeof(payload_blob) != 'blob'
                  OR length(payload_blob) > ?
                  OR attempts NOT BETWEEN 0 AND 100""",
            (_MAX_QUEUE_PAYLOAD_BYTES,),
        ).fetchone()[0]
        if (
            len(integrity) != 1
            or integrity[0][0] != "ok"
            or foreign_keys
            or payload_violations
            or not _queue_v3_purge_authorizations_valid(database)
            or not _queue_v3_payloads_valid(database)
        ):
            raise _migration_error(
                "queue_v3_validation_failed", "queue v3 database invariant failed"
            )
        return {
            "application_id": database.execute("PRAGMA application_id").fetchone()[0],
            "foreign_key_check": [],
            "integrity_check": "ok",
            "journal_mode": database.execute("PRAGMA journal_mode").fetchone()[0],
            "row_counts": _queue_v3_row_counts(database),
            "synchronous": database.execute("PRAGMA synchronous").fetchone()[0],
            "trusted_schema": database.execute("PRAGMA trusted_schema").fetchone()[0],
            "user_version": database.execute("PRAGMA user_version").fetchone()[0],
        }


def _require_active(
    deadline: float, cancelled: Callable[[], bool] | None = None
) -> None:
    if time.monotonic() >= deadline or bool(cancelled and cancelled()):
        raise TimeoutError("queue mutation deadline or cancellation reached")


class LeaseFenceError(RuntimeError):
    """Raised when a lease token no longer owns an unexpired task."""


class ResultConflictError(RuntimeError):
    """Raised when an operation ID already names different result bytes."""


class QueueOperationError(RuntimeError):
    """Operator-visible queue failure with a stable, non-sensitive code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    def __reduce__(self):
        """Keep the code and detail apart when the error crosses a process."""
        return (self.__class__, (self.code, self.detail))


@dataclass(frozen=True)
class PayloadValidation:
    raw: bytes
    input_hash: str
    payload: dict[str, object] | None
    code: str | None


def _reject_duplicate_payload_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate payload key")
        payload[key] = value
    return payload


def _reject_payload_constant(_value: str) -> object:
    raise ValueError("non-finite payload number")


def _parse_payload_integer(value: str) -> int:
    if len(value.encode("ascii")) > _MAX_QUEUE_STRING_BYTES:
        raise ValueError("payload integer token exceeds its bound")
    return int(value)


def _validate_payload_string(value: str, message: str) -> None:
    if len(value.encode("utf-8", errors="strict")) > _MAX_QUEUE_STRING_BYTES:
        raise ValueError(message)


def _validate_payload_array(value: list[object], *, depth: int) -> None:
    if len(value) > _MAX_QUEUE_CONTAINER_MEMBERS:
        raise ValueError("payload array exceeds its member bound")
    for item in value:
        _validate_payload_value(item, depth=depth + 1)


def _validate_payload_object(value: dict[object, object], *, depth: int) -> None:
    if len(value) > _MAX_QUEUE_CONTAINER_MEMBERS:
        raise ValueError("payload object exceeds its member bound")
    for key, item in value.items():
        _validate_payload_key(key)
        _validate_payload_value(item, depth=depth + 1)


def _validate_payload_key(key: object) -> None:
    if not isinstance(key, str):
        raise ValueError("payload keys must be strings")
    _validate_payload_string(key, "payload key exceeds its byte bound")


def _is_payload_integer(value: object) -> bool:
    """A value JSON keeps exactly: null, a boolean, or an integer."""
    return value is None or isinstance(value, (bool, int))


def _validate_payload_scalar(value: object) -> bool:
    """True when the value is a scalar we accept as it stands."""
    if _is_payload_integer(value):
        return True
    if isinstance(value, float):
        raise ValueError("payload floats are not permitted")
    if not isinstance(value, str):
        return False
    _validate_payload_string(value, "payload string exceeds its byte bound")
    return True


def _validate_payload_value(value: object, *, depth: int) -> None:
    if depth > _MAX_QUEUE_DEPTH:
        raise ValueError("payload exceeds its depth bound")
    if _validate_payload_scalar(value):
        return
    if isinstance(value, list):
        _validate_payload_array(value, depth=depth)
        return
    if isinstance(value, dict):
        _validate_payload_object(value, depth=depth)
        return
    raise ValueError("payload contains an unsupported value")


def validate_payload_blob(
    raw: bytes, stored_hash: str, *, parse: bool
) -> PayloadValidation:
    _check_payload_blob_arguments(raw, parse)
    input_hash = _payload_digest(raw)
    if not _payload_hash_matches(raw, stored_hash, input_hash):
        return PayloadValidation(raw, input_hash, None, "payload_hash_mismatch")
    if not parse:
        return PayloadValidation(raw, input_hash, None, None)
    payload = _parsed_canonical_payload(raw)
    if payload is None:
        return PayloadValidation(raw, input_hash, None, "payload_hash_mismatch")
    return PayloadValidation(raw, input_hash, payload, None)


def _check_payload_blob_arguments(raw: object, parse: object) -> None:
    if not isinstance(raw, bytes):
        raise TypeError("queue payload must be bytes")
    if not isinstance(parse, bool):
        raise TypeError("parse must be a boolean")


def _payload_digest(raw: bytes) -> str:
    """The payload's digest, read in bounded chunks."""
    digest = hashlib.sha256()
    view = memoryview(raw)
    for offset in range(0, len(view), 64 * 1024):
        digest.update(view[offset : offset + 64 * 1024])
    return digest.hexdigest()


def _payload_hash_matches(raw: bytes, stored_hash: object, input_hash: str) -> bool:
    """The payload is within bounds and hashes to the digest the row stored."""
    if len(raw) > _MAX_QUEUE_PAYLOAD_BYTES:
        return False
    if not isinstance(stored_hash, str):
        return False
    if re.fullmatch(r"[0-9a-f]{64}", stored_hash) is None:
        return False
    return input_hash == stored_hash


def _parsed_canonical_payload(raw: bytes) -> dict[str, object] | None:
    """The payload object, when these bytes are its own canonical encoding."""
    try:
        payload = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_payload_keys,
            parse_constant=_reject_payload_constant,
            parse_int=_parse_payload_integer,
        )
        _validate_payload_value(payload, depth=1)
        if not isinstance(payload, dict) or canonical_json_bytes(payload) != raw:
            raise ValueError("payload is not a canonical object")
    except (
        json.JSONDecodeError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        return None
    return payload


class MigrationBusy(QueueOperationError):
    """Raised when migration cannot prove exclusive legacy ownership."""


class LegacyBackendDisabled(QueueOperationError):
    """Raised when a legacy writer is used after migration started."""


class _InvalidArguments(Exception):
    pass


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _InvalidArguments

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            del message
            raise _InvalidArguments
        super().exit(status, message)


@dataclass(frozen=True)
class QueueFailure:
    error_code: str
    permanent: bool = False
    blocked_capability: str | None = None
    retry_after: float | datetime | str | None = None


@dataclass(frozen=True)
class DeferredResult:
    data: bytes


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    started_at: datetime
    finished_at: datetime
    outcome: str
    error_code: str | None


@dataclass(frozen=True)
class QueueTask:
    id: str
    kind: str
    handler_version: int
    payload: dict[str, object]
    input_hash: str
    dedupe_key: str | None
    state: str
    priority: int
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    attempts: int
    last_attempt_at: datetime | None
    lease_owner: str | None
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_heartbeat_at: datetime | None
    error_code: str | None
    blocked_capability: str | None
    result_reference: str | None
    result_sha256: str | None
    redrive_of: str | None
    attempt_history: tuple[AttemptRecord, ...]


@dataclass(frozen=True)
class QueueLease:
    id: str
    kind: str
    handler_version: int
    payload: dict[str, object]
    input_hash: str
    owner: str
    token: str
    expires_at: datetime
    attempt: int
    created_at: datetime
    last_attempt_at: datetime | None
    prior_attempts: int


@dataclass(frozen=True)
class MigrationReceipt:
    imported: int
    quarantined: int
    task_ids: tuple[str, ...]
    codes: tuple[str, ...]


@dataclass(frozen=True)
class QueueOwnerLease:
    state_root: Path
    role: str
    token: str
    pid: int
    epoch: int
    expires_at: datetime
    ttl_seconds: int


@dataclass(frozen=True)
class CaptureTaskBinding:
    task_id: str
    intent_id: str | None
    intent_sha256: str | None
    handler_version: int
    active_digest: str
    seal_digest: str | None


def _require_capture_intent_path(value: str, state: Literal["pending", "ready"]) -> None:
    restricted_relative_path(value, ("run/capture-intents",))
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{state} capture intent path is invalid")
    pattern = rf"run/capture-intents/{state}/[0-9a-f]{{2}}/[0-9a-f]{{64}}\.json"
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"{state} capture intent path is invalid")


def _require_lower_sha256(value: str, label: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be lowercase 64-hex")


def _require_capture_intent_size(value: int) -> None:
    if type(value) is not int:
        raise ValueError("capture intent byte size is invalid")
    if not 1 <= value <= _MAX_QUEUE_PAYLOAD_BYTES:
        raise ValueError("capture intent byte size is invalid")


def _require_capture_intent_descriptor(
    intent_id: str,
    pending_path: str,
    intent_sha256: str,
    byte_size: int,
) -> None:
    _require_lower_sha256(intent_id, "intent_id")
    _require_lower_sha256(intent_sha256, "intent_sha256")
    _require_capture_intent_size(byte_size)
    _require_capture_intent_path(pending_path, "pending")
    expected = f"run/capture-intents/pending/{intent_id[:2]}/{intent_id}.json"
    if pending_path != expected:
        raise ValueError("capture intent descriptor is invalid")


def _require_capture_decision_path(value: str, intent_id: str, stage: str) -> None:
    restricted_relative_path(value, ("run/queue-results",))
    expected = _capture_decision_relative_path(intent_id, stage)
    if value != expected:
        raise ValueError("decision path is invalid")


def _capture_decision_relative_path(intent_id: str, stage: str) -> str:
    key = sha256_bytes(canonical_json_bytes({"intent_id": intent_id, "stage": stage}))
    return f"run/queue-results/capture-decision-{key}.json"


def _capture_semantic_seal_digest(
    task_id: str, intent_id: str, stage: str, active_link_digest: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "active_digest": active_link_digest,
                "consumer_id": f"{intent_id}:{stage}",
                "consumer_kind": "semantic-decision",
                "task_id": task_id,
            }
        )
    )


def _require_indexed_capture_decision(
    row: sqlite3.Row,
    active: CaptureTaskBinding,
    *,
    intent_id: str,
    stage: str,
    active_link_digest: str,
) -> None:
    decision_path = _capture_decision_relative_path(intent_id, stage)
    seal_digest = _capture_semantic_seal_digest(
        active.task_id, intent_id, stage, active_link_digest
    )
    actual = (
        active.intent_id,
        active.active_digest,
        active.seal_digest,
        row["decision_path"],
        row["active_link_digest"],
        row["publication_state"],
    )
    expected = (
        intent_id,
        active_link_digest,
        seal_digest,
        decision_path,
        active_link_digest,
        "published",
    )
    if actual != expected:
        raise QueueOperationError("semantic_decision_conflict")


def _indexed_capture_decision_published_at(row: sqlite3.Row) -> datetime:
    published_at = _parse_timestamp(str(row["published_at"]))
    if published_at is None:
        raise QueueOperationError("semantic_decision_conflict")
    return published_at


def _read_indexed_capture_decision(
    state_root: Path, row: sqlite3.Row
) -> tuple[str, str]:
    decision_path = str(row["decision_path"])
    decision_sha256 = str(row["decision_sha256"])
    _require_lower_sha256(decision_sha256, "decision_sha256")
    data = read_runtime_bytes(
        state_root / decision_path,
        state_root,
        max_bytes=1024 * 1024,
        owner_only=True,
    )
    if sha256_bytes(data) != decision_sha256:
        raise QueueOperationError("semantic_decision_conflict")
    return decision_path, decision_sha256


def _require_capture_terminal_path(value: str, intent_id: str) -> None:
    restricted_relative_path(value, ("run/queue-results",))
    expected = f"run/queue-results/capture-{intent_id}.json"
    if value != expected:
        raise ValueError("capture terminal path is invalid")


def _capture_terminal_disposition_fields(kind: object) -> set[str] | None:
    if not isinstance(kind, str):
        return None
    return _CAPTURE_TERMINAL_DISPOSITION_FIELDS.get(kind)


def _existing_capture_publication_state(
    row: sqlite3.Row | None,
    *,
    pending_path: str,
    ready_path: str,
    intent_sha256: str,
    byte_size: int,
) -> str:
    if row is None:
        raise QueueOperationError("capture_intent_conflict")
    state = str(row["publication_state"])
    expected_paths = {"pending": pending_path, "ready": ready_path}
    matches = (
        expected_paths.get(state) == row["relative_path"]
        and row["intent_sha256"] == intent_sha256
        and row["byte_size"] == byte_size
    )
    if not matches:
        raise QueueOperationError("capture_intent_conflict")
    return state


def _require_capture_replay_task(
    row: sqlite3.Row,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    dedupe_key: str,
) -> None:
    stored = bytes(row["payload_blob"])
    validation = validate_payload_blob(stored, str(row["input_hash"]), parse=True)
    actual = (
        validation.code,
        row["kind"],
        row["handler_version"],
        row["input_hash"],
        row["dedupe_key"],
        stored,
    )
    expected = (None, kind, handler_version, sha256_bytes(payload_bytes), dedupe_key, payload_bytes)
    if actual != expected:
        raise QueueOperationError("capture_replay_conflict")


@dataclass(frozen=True)
class TaskFence:
    task_id: str
    mode: Literal["worker", "queue-operator"]
    token: str
    epoch: int
    owner: OwnerLease
    expires_at: datetime


@dataclass(frozen=True)
class SemanticDecision:
    task_id: str
    intent_id: str
    stage: Literal["flush", "feedback", "feedback-verify"]
    decision_path: str
    decision_sha256: str
    active_link_digest: str
    seal_digest: str
    published_at: datetime


@dataclass(frozen=True)
class CorruptExportProgress:
    task_id: str
    operation_id: str
    state: Literal["quarantine_pending", "quarantined", "blocked"]
    pages_written: int
    links_exported: int
    complete: bool
    code: str | None


@dataclass(frozen=True)
class CorruptPurgeProgress:
    task_id: str
    operation_id: str
    state: Literal["purge_pending", "purged", "blocked"]
    pages_written: int
    links_deleted: int
    complete: bool
    code: str | None


@dataclass(frozen=True)
class SourceFence:
    daily_id: str
    source_digest: str
    token: str
    owner_pid: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class WorkerSummary:
    processed: int
    succeeded: int
    failed: int
    dead: int
    skipped: int
    remaining_eligible: int = 0


@dataclass(frozen=True)
class PurgeReceipt:
    purged: int
    task_ids: tuple[str, ...]


@dataclass(frozen=True)
class RestoreReceipt:
    restored: int
    task_ids: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _CapturePurgeArtifact:
    source_path: str
    archive_path: str
    sha256: str


@dataclass(frozen=True)
class _CapturePurgeEvidence:
    task_id: str
    intent_id: str
    intent: _CapturePurgeArtifact
    decisions: tuple[_CapturePurgeArtifact, ...]
    terminal_path: str


@dataclass(frozen=True)
class _OrdinaryPurgePlan:
    cutoff: str
    export: Path
    task_ids: tuple[str, ...]
    records: tuple[dict[str, object], ...]
    records_bytes: bytes
    capture_evidence: tuple[_CapturePurgeEvidence, ...]
    manifest_bytes: bytes | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: str | None) -> datetime | None:
    return _as_utc(datetime.fromisoformat(value)) if value is not None else None


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
    return normalized in _SECRET_KEYS or normalized.endswith(
        ("_api_key", "_authorization", "_cookie", "_credential", "_password", "_secret", "_token")
    )


def _require_task_kind(kind: object) -> None:
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a non-empty bounded string")
    if len(kind.encode("utf-8")) > 64:
        raise ValueError("kind must be a non-empty bounded string")


def _require_bounded_int(value: object, low: int, high: int, message: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(message)
    if not low <= value <= high:
        raise ValueError(message)


def _require_dedupe_key(dedupe_key: object) -> None:
    if dedupe_key is None:
        return
    if not isinstance(dedupe_key, str) or not dedupe_key:
        raise ValueError("dedupe_key must be a non-empty bounded string")
    if len(dedupe_key.encode("utf-8")) > 512:
        raise ValueError("dedupe_key must be a non-empty bounded string")


def _unsafe_link_path(logical_path: object) -> bool:
    if not isinstance(logical_path, str) or not logical_path:
        return True
    if len(logical_path.encode("utf-8")) > 4096:
        return True
    if logical_path.startswith(("/", "\\")) or "\\" in logical_path:
        return True
    return any(part in {"", ".", ".."} for part in logical_path.split("/"))


def _link_pair(item: object) -> tuple[object, object]:
    if not isinstance(item, tuple) or len(item) != 2:
        raise ValueError("source links must be path and digest pairs")
    return item


def _normalized_source_link(item: object) -> tuple[str, str]:
    logical_path, source_digest = _link_pair(item)
    if _unsafe_link_path(logical_path):
        raise ValueError("source link path is invalid")
    if not isinstance(source_digest, str):
        raise ValueError("source link digest is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
        raise ValueError("source link digest is invalid")
    return logical_path, source_digest


def _normalized_source_links(
    source_links: tuple[tuple[str, str], ...] | list[tuple[str, str]],
) -> list[tuple[str, str]]:
    normalized = [_normalized_source_link(item) for item in source_links]
    if len(normalized) != len(set(normalized)):
        raise ValueError("source links must be unique")
    return normalized


def _require_enqueue_arguments(
    kind: object,
    handler_version: object,
    priority: object,
    dedupe_key: object,
) -> None:
    _require_task_kind(kind)
    _require_bounded_int(
        handler_version,
        1,
        2_147_483_647,
        "handler_version must be a positive bounded integer",
    )
    _require_bounded_int(
        priority, -100, 100, "priority must be an integer from -100 to 100"
    )
    _require_dedupe_key(dedupe_key)


def _validated_payload_bytes(payload: Mapping[str, object]) -> tuple[bytes, str]:
    payload_bytes = canonical_json_bytes(_redact_payload(dict(payload)))
    input_hash = sha256_bytes(payload_bytes)
    validation = validate_payload_blob(payload_bytes, input_hash, parse=True)
    if validation.code is not None:
        raise ValueError(validation.code)
    return payload_bytes, input_hash


def _same_enqueued_task(
    existing: sqlite3.Row,
    existing_bytes: bytes,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    input_hash: str,
) -> bool:
    if existing["kind"] != kind or existing["handler_version"] != handler_version:
        return False
    if existing["input_hash"] != input_hash:
        return False
    return existing_bytes == payload_bytes


def _require_legacy_enqueue_arguments(
    kind: object, handler_version: object, priority: object, dedupe_key: object
) -> None:
    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a non-empty string")
    _require_bounded_int(
        handler_version,
        1,
        2_147_483_647,
        "handler_version must be a positive integer",
    )
    _require_bounded_int(
        priority, -100, 100, "priority must be an integer from -100 to 100"
    )
    if dedupe_key is None:
        return
    if not isinstance(dedupe_key, str) or not dedupe_key:
        raise ValueError("dedupe_key must be a non-empty string")


def _legacy_payload_bytes(row: sqlite3.Row) -> bytes:
    try:
        return str(row["payload_json"]).encode("utf-8", errors="strict")
    except UnicodeError:
        return b""


def _require_capture_identity(
    intent_id: object, intent_sha256: object, intent_path: object
) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", str(intent_id)) is None:
        raise ValueError("intent_id must be lowercase 64-hex")
    if re.fullmatch(r"[0-9a-f]{64}", str(intent_sha256)) is None:
        raise ValueError("intent_sha256 must be lowercase 64-hex")
    if not isinstance(intent_path, str):
        raise ValueError("intent_path is invalid")
    if len(intent_path.encode("utf-8")) > 4096:
        raise ValueError("intent_path is invalid")


def _require_capture_fence(
    capture_fence: object, intent_id: str, owner: object, fence_type: type
) -> None:
    if not isinstance(capture_fence, fence_type):
        raise ValueError("capture fence does not match the intent and owner")
    if capture_fence.intent_id != intent_id or capture_fence.mode != "capture":
        raise ValueError("capture fence does not match the intent and owner")
    if capture_fence.owner != owner:
        raise ValueError("capture fence does not match the intent and owner")


def _capture_link_record(
    task_id: str, intent_id: str, intent_sha256: str, handler_version: int
) -> dict[str, object]:
    return {
        "schema_version": "capture-task-link/v1",
        "task_id": task_id,
        "intent_id": intent_id,
        "intent_sha256": intent_sha256,
        "handler_version": handler_version,
    }


def _matching_capture_intent(
    intent: sqlite3.Row | None, intent_path: str, intent_sha256: str
) -> bool:
    if intent is None:
        return False
    if intent["relative_path"] != intent_path:
        return False
    if intent["intent_sha256"] != intent_sha256:
        return False
    return intent["publication_state"] == "ready"


def _validated_capture_payload(payload: Mapping[str, object]) -> tuple[bytes, str]:
    payload_bytes = canonical_json_bytes(_redact_payload(dict(payload)))
    input_hash = sha256_bytes(payload_bytes)
    if validate_payload_blob(payload_bytes, input_hash, parse=True).code is not None:
        raise ValueError("payload_hash_mismatch")
    return payload_bytes, input_hash


def _purge_states(include_dead: bool) -> tuple[str, ...]:
    if include_dead:
        return ("succeeded", "cancelled", "dead")
    return ("succeeded", "cancelled")


def _require_export_parent(parent: Path) -> None:
    if not parent.exists():
        parent.mkdir(parents=True)
        _harden_owner_only(parent, 0o700)
    if parent.is_symlink() or not parent.is_dir():
        raise QueueOperationError("export_parent_permissions_invalid")
    if not _is_owner_only(parent):
        raise QueueOperationError("export_parent_permissions_invalid")


def _purge_selection_changed(
    current: list, task_ids: tuple[str, ...], states: tuple[str, ...], cutoff_stamp: str
) -> bool:
    if len(current) != len(task_ids):
        return True
    return any(
        row["state"] not in states or row["updated_at"] >= cutoff_stamp
        for row in current
    )


def _require_semantic_decision_fences(
    task_id: str,
    intent_id: str,
    stage: str,
    decision_sha256: str,
    task_fence: object,
    intent_fence: object,
    owner: object,
    fence_type: type,
    owner_type: type,
) -> None:
    if stage not in {"flush", "feedback", "feedback-verify"}:
        raise ValueError("semantic decision stage is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", str(decision_sha256)) is None:
        raise ValueError("decision_sha256 must be lowercase 64-hex")
    if not isinstance(task_fence, TaskFence) or task_fence.task_id != task_id:
        raise ValueError("task fence does not match")
    _require_worker_intent_fence(intent_fence, intent_id, fence_type)
    _require_matching_decision_owner(owner, task_fence, intent_fence, owner_type)


def _require_worker_intent_fence(
    intent_fence: object, intent_id: str, fence_type: type
) -> None:
    if not isinstance(intent_fence, fence_type):
        raise ValueError("intent fence does not match")
    if intent_fence.intent_id != intent_id or intent_fence.mode != "worker":
        raise ValueError("intent fence does not match")


def _require_matching_decision_owner(
    owner: object, task_fence: object, intent_fence: object, owner_type: type
) -> None:
    if not isinstance(owner, owner_type):
        raise ValueError("semantic decision owner does not match fences")
    if task_fence.owner != owner or intent_fence.owner != owner:
        raise ValueError("semantic decision owner does not match fences")


def _semantic_seal_digest(
    task_id: str, intent_id: str, stage: str, active_link_digest: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "active_digest": active_link_digest,
                "consumer_id": f"{intent_id}:{stage}",
                "consumer_kind": "semantic-decision",
                "task_id": task_id,
            }
        )
    )


def _semantic_decision_matches(
    existing: sqlite3.Row,
    intent_id: str,
    stage: str,
    seal_digest: str,
    decision_path: str,
    decision_sha256: str,
    active_link_digest: str,
) -> bool:
    if existing["consumer_kind"] != "semantic-decision":
        return False
    if existing["consumer_id"] != f"{intent_id}:{stage}":
        return False
    if existing["seal_digest"] != seal_digest:
        return False
    if existing["decision_path"] != decision_path:
        return False
    if existing["decision_sha256"] != decision_sha256:
        return False
    return existing["active_link_digest"] == active_link_digest


def _purge_progress(
    task_id: str,
    operation_id: str,
    pages_written: int,
    links_deleted: int,
    *,
    state: str,
    code: str | None = None,
) -> CorruptPurgeProgress:
    return CorruptPurgeProgress(
        task_id=task_id,
        operation_id=operation_id,
        state=state,
        pages_written=pages_written,
        links_deleted=links_deleted,
        complete=False,
        code=code,
    )


def _blocked_or_pending(blocker_code: str | None) -> str:
    if blocker_code is None:
        return "purge_pending"
    return "blocked"


def _receipt_matches_disposition(
    receipt: dict,
    disposition: sqlite3.Row,
    operation: sqlite3.Row,
    operation_id: str,
    task_id: str,
) -> bool:
    if receipt["operation_id"] != operation_id or receipt["task_id"] != task_id:
        return False
    if receipt["package_key"] != disposition["disposition_key"]:
        return False
    if receipt["manifest_sha256"] != disposition["manifest_sha256"]:
        return False
    if receipt["disposition_sha256"] != disposition["disposition_sha256"]:
        return False
    if receipt["original_frozen_root"] != disposition["original_frozen_root"]:
        return False
    return _receipt_matches_operation(receipt, operation)


def _receipt_matches_operation(receipt: dict, operation: sqlite3.Row) -> bool:
    if receipt["purge_page_count"] != operation["page_count"]:
        return False
    if receipt["final_rolling_root"] != operation["rolling_root"]:
        return False
    return receipt["final_generation"] == operation["expected_generation"]


def _parent_state_ready_for_delete(
    operation: sqlite3.Row, task: sqlite3.Row
) -> bool:
    if operation["state"] != "receipt-published":
        return False
    if task["state"] != "purge_pending":
        return False
    return int(task["lineage_generation"]) == int(operation["expected_generation"])


def _exported_bytes_match(
    raw: bytes,
    history: bytes,
    current_history: bytes,
    task: sqlite3.Row,
    disposition: sqlite3.Row,
) -> bool:
    if sha256_bytes(raw) != disposition["raw_sha256"]:
        return False
    if sha256_bytes(bytes(task["payload_blob"])) != disposition["raw_sha256"]:
        return False
    if sha256_bytes(history) != disposition["history_sha256"]:
        return False
    return sha256_bytes(current_history) == disposition["history_sha256"]


def _require_repair_owner(owner: object, owner_type: type, message: str) -> None:
    if not isinstance(owner, owner_type) or owner.role != "repair":
        raise ValueError(message)


def _require_task_identifier(task_id: object) -> None:
    if not isinstance(task_id, str):
        raise ValueError("task ID is invalid")
    if not 1 <= len(task_id.encode("utf-8")) <= 256:
        raise ValueError("task ID is invalid")


def _require_matching_purge_operation(
    existing_operation: sqlite3.Row, task: sqlite3.Row, operation_id: str
) -> None:
    if existing_operation["operation_id"] != operation_id:
        raise QueueOperationError("corrupt_purge_conflict")
    if task["state"] != "purge_pending":
        raise QueueOperationError("corrupt_purge_conflict")


def _blocked_purge(task_id: str, code: str) -> CorruptPurgeProgress:
    return _purge_progress(task_id, "", 0, 0, state="blocked", code=code)


def _redact_payload(value: object) -> object:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, (list, tuple)):
        return [_redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_secret_key(key) else _redact_payload(item)
            for key, item in value.items()
        }
    return value


def _harden_owner_only(path: Path, mode: int) -> None:
    if os.name == "nt":
        from markdown_transaction import _harden_windows_acl

        _harden_windows_acl(path)
        return
    if not _set_owner_only(path, mode):
        raise PermissionError(f"could not apply owner-only permissions to {path}")


def _windows_acl_lines(path: Path) -> list[str] | None:
    """The access control entries icacls reports, or None when it refuses."""
    from markdown_transaction import _acl_output_text, _run_acl_command

    try:
        verified = _run_acl_command(["icacls", str(path)])
    except Exception:  # noqa: BLE001 - validation is fail-closed
        return None
    if verified.returncode != 0:
        return None
    return [
        line.strip()
        for line in _acl_output_text(verified.stdout).splitlines()
        if ":(" in line
    ]


def _acl_owner_lines(acl_lines: list[str], folded: str) -> list[str]:
    return [line for line in acl_lines if folded in line.casefold()]


def _acl_is_owner_only(acl_lines: list[str], identity: str) -> bool:
    """One full-control entry for us, and nothing else at all."""
    folded = identity.casefold()
    owner_lines = _acl_owner_lines(acl_lines, folded)
    if len(owner_lines) != 1 or "(F)" not in owner_lines[0]:
        return False
    return len(owner_lines) == len(acl_lines)


def _is_owner_only_windows(path: Path) -> bool:
    from markdown_transaction import _windows_acl_identity

    acl_lines = _windows_acl_lines(path)
    if acl_lines is None:
        return False
    return _acl_is_owner_only(acl_lines, _windows_acl_identity())


def _is_owner_only_posix(path: Path) -> bool:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode & 0o077 == 0 and mode & 0o600 == 0o600


def _is_owner_only(path: Path) -> bool:
    if os.name == "nt":
        return _is_owner_only_windows(path)
    return _is_owner_only_posix(path)


def _validate_retry_policy(
    max_attempts: int, retry_base_seconds: int, retry_cap_seconds: int
) -> None:
    for name, value in (
        ("max_attempts", max_attempts),
        ("retry_base_seconds", retry_base_seconds),
        ("retry_cap_seconds", retry_cap_seconds),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
    if not 1 <= max_attempts <= _MAX_RUNTIME_ATTEMPTS:
        raise ValueError(
            f"max_attempts must be between 1 and {_MAX_RUNTIME_ATTEMPTS}"
        )
    if retry_base_seconds <= 0:
        raise ValueError("retry_base_seconds must be positive")
    if retry_cap_seconds <= 0:
        raise ValueError("retry_cap_seconds must be positive")
    if retry_base_seconds > retry_cap_seconds:
        raise ValueError("retry_base_seconds must not exceed retry_cap_seconds")


def _validate_worker_policy(
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    _validate_retry_policy(max_attempts, retry_base_seconds, retry_cap_seconds)
    for name, value in (
        ("lease_seconds", lease_seconds),
        ("heartbeat_seconds", heartbeat_seconds),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if heartbeat_seconds >= lease_seconds:
        raise ValueError("heartbeat_seconds must be less than lease_seconds")


def _check_result_request(operation_id: str, result: bytes) -> None:
    """What a caller has to hand over before anything is written."""
    if not operation_id:
        raise ValueError("operation_id must be non-empty")
    if not isinstance(result, bytes):
        raise TypeError("result must be bytes")
    if len(result) > _MAX_RESULT_BYTES:
        raise ValueError("result exceeds maximum queue result size")


def _check_result_operation(row: Mapping[str, object], operation_id: str) -> None:
    """A lease publishes for exactly one operation."""
    existing = row["result_operation_id"]
    if existing is not None and existing != operation_id:
        raise ResultConflictError("lease already published a different operation")


def _write_result_file(path: Path, payload: bytes) -> None:
    """Write the result to a fresh owner-only temporary file, durably."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        _harden_owner_only(path, 0o600)
        handle = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _link_result(temporary: Path, target: Path) -> bool:
    """Link the staged bytes into place; False when a result was already there."""
    if target.exists() or target.is_symlink():
        return False
    try:
        os.link(temporary, target)
    except FileExistsError:
        return False
    return True


def _discard_temporary(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


@dataclass(frozen=True)
class _CorruptEvidence:
    """The immutable bytes and digests one corrupt export is derived from."""

    raw: bytes
    history: bytes
    metadata: bytes
    raw_sha256: str
    history_sha256: str
    metadata_sha256: str
    disposition_key: str
    lineage_generation: int


def _check_quarantine_names(task_id: str, reason: str) -> None:
    """A quarantine names one task and says why, both within bounds."""
    if not isinstance(task_id, str) or not 1 <= len(task_id.encode("utf-8")) <= 256:
        raise ValueError("task ID is invalid")
    if not isinstance(reason, str) or not 1 <= len(reason.encode("utf-8")) <= 4096:
        raise ValueError("quarantine reason is invalid")


def _attempt_history_entry(item: Mapping[str, object]) -> dict[str, object]:
    return {
        "attempt": int(item["attempt"]),
        "started_at": str(item["started_at"]),
        "finished_at": str(item["finished_at"]),
        "outcome": str(item["outcome"]),
        "error_code": item["error_code"],
    }


def _corrupt_metadata(task_id: str, row: Mapping[str, object]) -> bytes:
    return canonical_json_bytes(
        {
            "task_id": task_id,
            "kind": str(row["kind"]),
            "handler_version": int(row["handler_version"]),
            "input_hash": str(row["input_hash"]),
            "state": "dead",
            "error_code": row["error_code"],
            "lineage_generation": int(row["lineage_generation"]),
        }
    )


def _corrupt_disposition_key(
    *,
    actor: str,
    history_sha256: str,
    metadata_sha256: str,
    raw_sha256: str,
    reason: str,
    task_id: str,
) -> str:
    """One key for exactly this task, these bytes, this actor and this reason."""
    return sha256_bytes(
        canonical_json_bytes(
            {
                "actor_identity": actor,
                "history_sha256": history_sha256,
                "metadata_sha256": metadata_sha256,
                "raw_sha256": raw_sha256,
                "reason": reason,
                "task_id": task_id,
            }
        )
    )


def _corrupt_child_leaf_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child something else was redriven from is not a leaf."""
    row = database.execute(
        "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (str(child["id"]),)
    ).fetchone()
    return "corrupt_child_not_leaf" if row is not None else None


def _corrupt_child_state_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child still running, or one whose kind is kept forever."""
    if child["state"] not in _TERMINAL_STATES:
        return "corrupt_child_nonterminal"
    if child["state"] == "dead" and DEFAULTS.dead_task_retention_days is None:
        return "corrupt_child_retained"
    return None


def _child_retention_days(child: sqlite3.Row) -> int | None:
    if child["state"] == "dead":
        return DEFAULTS.dead_task_retention_days
    return DEFAULTS.queue_result_retention_days


def _corrupt_child_retention_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child whose retention window has not run out yet."""
    updated_at = _parse_timestamp(str(child["updated_at"]))
    retention_days = _child_retention_days(child)
    if updated_at is None or retention_days is None:
        return "corrupt_child_retention_active"
    if updated_at + timedelta(days=retention_days) > now:
        return "corrupt_child_retention_active"
    return None


def _corrupt_child_fence_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    row = database.execute(
        "SELECT 1 FROM task_fences WHERE task_id=? LIMIT 1", (str(child["id"]),)
    ).fetchone()
    return "corrupt_child_fenced" if row is not None else None


def _active_binding_or_block(
    queue: MemoryQueue, database: sqlite3.Connection, child_id: str
) -> tuple[object | None, str | None]:
    """The child's active capture binding, or why it cannot be read."""
    link_exists = (
        database.execute(
            "SELECT 1 FROM capture_task_links WHERE task_id=?", (child_id,)
        ).fetchone()
        is not None
    )
    try:
        return queue.active_capture_binding(database, child_id), None
    except QueueOperationError:
        if link_exists:
            return None, "corrupt_child_intent_unresolved"
        return None, None


def _corrupt_child_intent_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child whose capture intent has no terminal record yet."""
    child_id = str(child["id"])
    binding, blocker = _active_binding_or_block(queue, database, child_id)
    if blocker is not None:
        return blocker
    if binding is None or binding.intent_id is None:
        return None
    if queue._capture_terminal_blocker(database, child_id, binding) is None:
        return None
    return "corrupt_child_intent_unresolved"


def _corrupt_child_decision_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    row = database.execute(
        """SELECT 1 FROM capture_task_link_seals
           WHERE task_id=? AND consumer_kind='semantic-decision' LIMIT 1""",
        (str(child["id"]),),
    ).fetchone()
    return "corrupt_child_decision_retained" if row is not None else None


def _corrupt_child_result_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    fields = ("result_reference", "result_sha256", "result_operation_id")
    if any(child[field] is not None for field in fields):
        return "corrupt_child_result_retained"
    return None


_CORRUPT_CHILD_CHECKS = (
    _corrupt_child_leaf_blocker,
    _corrupt_child_state_blocker,
    _corrupt_child_retention_blocker,
    _corrupt_child_fence_blocker,
    _corrupt_child_intent_blocker,
    _corrupt_child_decision_blocker,
    _corrupt_child_result_blocker,
)


def _owner_only_directory(package: Path) -> bool:
    """A real, owner-only directory — not a link, a reparse point, or a file."""
    metadata = package.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode):
        return False
    if getattr(metadata, "st_file_attributes", 0) & reparse_flag:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    return _is_owner_only(package)


def _validated_corrupt_records(
    package: Path,
) -> tuple[bytes, bytes, dict[str, object], dict[str, object]]:
    """The manifest and disposition bytes, and the objects they parse into."""
    schemas = Path(__file__).with_name("schemas")
    manifest_bytes = _read_stable_owner_file(package / "manifest.json", 64 * 1024)
    disposition_bytes = _read_stable_owner_file(
        package / "disposition.json", 64 * 1024
    )
    manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    record = json.loads(disposition_bytes.decode("utf-8", errors="strict"))
    validate_schema(manifest, schemas / "corrupt-task-manifest-v1.json")
    validate_schema(record, schemas / "corrupt-task-disposition-v1.json")
    return manifest_bytes, disposition_bytes, manifest, record


def _corrupt_records_match(
    manifest_bytes: bytes,
    disposition_bytes: bytes,
    manifest: Mapping[str, object],
    record: Mapping[str, object],
    disposition: sqlite3.Row,
) -> bool:
    """The package's own records agree with the row that points at them."""
    if sha256_bytes(manifest_bytes) != disposition["manifest_sha256"]:
        return False
    if sha256_bytes(disposition_bytes) != disposition["disposition_sha256"]:
        return False
    task_id = disposition["task_id"]
    package_path = disposition["package_path"]
    named = (
        manifest["task_id"],
        record["task_id"],
        manifest["package_path"],
        record["package_path"],
    )
    return named == (task_id, task_id, package_path, package_path)


def _corrupt_payload_matches(
    package: Path, manifest: Mapping[str, object], disposition: sqlite3.Row
) -> bool:
    """The exported bytes still hash to what the manifest recorded."""
    raw = _read_stable_owner_file(package / "payload.bin", _MAX_QUEUE_PAYLOAD_BYTES)
    history = _read_stable_owner_file(
        package / "attempt-history.json", _MAX_EXPORT_METADATA_BYTES
    )
    metadata = _read_stable_owner_file(
        package / "task-metadata.json", _MAX_EXPORT_METADATA_BYTES
    )
    digests = (sha256_bytes(raw), sha256_bytes(history), sha256_bytes(metadata))
    expected = (
        manifest["raw_sha256"],
        manifest["history_sha256"],
        manifest["metadata_sha256"],
    )
    if digests != expected:
        return False
    return manifest["rolling_root"] == disposition["original_frozen_root"]


def _source_fence_row_matches(row: sqlite3.Row, fence: SourceFence) -> bool:
    """The stored fence names the same source, token and owning process."""
    stored = (
        str(row["daily_id"]),
        str(row["source_digest"]),
        str(row["token"]),
        int(row["owner_pid"]),
        str(row["acquired_at"]),
    )
    expected = (
        fence.daily_id,
        fence.source_digest,
        fence.token,
        os.getpid(),
        _timestamp(fence.acquired_at),
    )
    return stored == expected and fence.owner_pid == os.getpid()


def _check_capture_intent_digest(value: str, label: str) -> None:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{label} must be lowercase 64-hex")


def _check_capture_intent_relative_path(intent_path: object) -> None:
    """A published intent lives under the capture-intent directory, bounded."""
    if not isinstance(intent_path, str) or "\\" in intent_path:
        raise ValueError("intent_path is invalid")
    if not intent_path.startswith("run/capture-intents/"):
        raise ValueError("intent_path is invalid")
    if len(intent_path.encode("utf-8")) > 4096:
        raise ValueError("intent_path is invalid")


def _check_capture_intent_size(byte_size: object) -> None:
    if isinstance(byte_size, bool) or not isinstance(byte_size, int):
        raise ValueError("intent byte size is invalid")
    if not 1 <= byte_size <= _MAX_QUEUE_PAYLOAD_BYTES:
        raise ValueError("intent byte size is invalid")


def _check_capture_intent_descriptor(
    intent_id: str, intent_path: str, intent_sha256: str, byte_size: int
) -> None:
    """Everything a publication has to name, before anything is written."""
    _check_capture_intent_digest(intent_id, "intent_id")
    _check_capture_intent_digest(intent_sha256, "intent_sha256")
    _check_capture_intent_relative_path(intent_path)
    _check_capture_intent_size(byte_size)


def _check_ready_intent_identity(
    existing: sqlite3.Row, intent_path: str, intent_sha256: str, byte_size: int
) -> None:
    """A republication has to name exactly the intent already recorded ready."""
    stored = (
        existing["relative_path"],
        existing["intent_sha256"],
        existing["byte_size"],
        existing["publication_state"],
    )
    if stored != (intent_path, intent_sha256, byte_size, "ready"):
        raise QueueOperationError("capture_intent_conflict")


_CaptureLeaf = tuple[str, str | None, str | None, int]


def _superseded_digests(resolutions: Sequence[sqlite3.Row]) -> set[str]:
    """Every digest a later resolution replaced."""
    return {
        str(row["supersedes_digest"])
        for row in resolutions
        if row["supersedes_digest"] is not None
    }


def _resolution_leaf(
    database: sqlite3.Connection, link: sqlite3.Row, resolution: sqlite3.Row
) -> _CaptureLeaf:
    """One unsuperseded resolution, with the intent it selected if it named one."""
    digest = str(resolution["resolution_digest"])
    selected = resolution["selected_intent_id"]
    if selected is None:
        return digest, None, None, int(link["handler_version"])
    intent = database.execute(
        "SELECT * FROM capture_intents WHERE intent_id=?", (selected,)
    ).fetchone()
    if intent is None:
        raise QueueOperationError("capture_link_conflicted")
    return (
        digest,
        str(intent["intent_id"]),
        str(intent["intent_sha256"]),
        int(link["handler_version"]),
    )


def _capture_link_leaves(
    database: sqlite3.Connection,
    link: sqlite3.Row,
    resolutions: Sequence[sqlite3.Row],
) -> list[_CaptureLeaf]:
    """Everything in this link's history that nothing else superseded."""
    superseded = _superseded_digests(resolutions)
    leaves: list[_CaptureLeaf] = []
    if str(link["link_digest"]) not in superseded:
        leaves.append(
            (
                str(link["link_digest"]),
                str(link["intent_id"]),
                str(link["intent_sha256"]),
                int(link["handler_version"]),
            )
        )
    for resolution in resolutions:
        if str(resolution["resolution_digest"]) in superseded:
            continue
        leaves.append(_resolution_leaf(database, link, resolution))
    return leaves


def _capture_binding_of(
    database: sqlite3.Connection, task_id: str
) -> CaptureTaskBinding:
    """The one active binding this task's link history leaves behind."""
    link = database.execute(
        "SELECT * FROM capture_task_links WHERE task_id=?", (task_id,)
    ).fetchone()
    if link is None:
        raise QueueOperationError("capture_link_conflicted")
    resolutions = database.execute(
        """SELECT * FROM capture_task_link_resolutions
           WHERE task_id=? ORDER BY created_at,resolution_digest""",
        (task_id,),
    ).fetchall()
    leaves = _capture_link_leaves(database, link, resolutions)
    if len(leaves) != 1:
        raise QueueOperationError("capture_link_conflicted")
    active_digest, intent_id, intent_sha256, handler_version = leaves[0]
    seal = database.execute(
        "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone()
    return CaptureTaskBinding(
        task_id=task_id,
        intent_id=intent_id,
        intent_sha256=intent_sha256,
        handler_version=handler_version,
        active_digest=active_digest,
        seal_digest=None if seal is None else str(seal["seal_digest"]),
    )


def _corrupt_manifest(
    operation: sqlite3.Row,
    *,
    operation_id: str,
    task_id: str,
    disposition_key: str,
) -> dict[str, object]:
    """The manifest describing everything the export produced."""
    manifest = {
        "schema_version": "corrupt-task-manifest/v1",
        "operation_id": operation_id,
        "task_id": task_id,
        "package_key": disposition_key,
        "package_path": f"run/queue-results/corrupt-{disposition_key}",
        "raw_sha256": str(operation["raw_sha256"]),
        "history_sha256": str(operation["history_sha256"]),
        "metadata_sha256": str(operation["metadata_sha256"]),
        "lineage_generation": int(operation["lineage_generation"]),
        "link_count": int(operation["link_count"]),
        "page_count": int(operation["page_count"]),
        "rolling_root": str(operation["rolling_root"]),
    }
    validate_schema(
        manifest,
        Path(__file__).with_name("schemas") / "corrupt-task-manifest-v1.json",
    )
    return manifest


def _corrupt_disposition_record(
    *,
    operation_id: str,
    task_id: str,
    disposition_key: str,
    manifest_sha256: str,
    active_link_digest: str,
    actor: str,
    reason: str,
    disposed_at: str,
) -> dict[str, object]:
    """The record that retires the task and points at its evidence."""
    record = {
        "schema_version": "corrupt-task-disposition/v1",
        "operation_id": operation_id,
        "task_id": task_id,
        "package_key": disposition_key,
        "package_path": f"run/queue-results/corrupt-{disposition_key}",
        "manifest_path": (
            f"run/queue-results/corrupt-{disposition_key}/manifest.json"
        ),
        "manifest_sha256": manifest_sha256,
        "active_link_digest": active_link_digest,
        "actor_identity": actor,
        "reason": reason,
        "disposed_at": disposed_at,
    }
    validate_schema(
        record,
        Path(__file__).with_name("schemas") / "corrupt-task-disposition-v1.json",
    )
    return record


def _verify_corrupt_package_bytes(
    package: Path,
    *,
    raw: bytes,
    history: bytes,
    metadata: bytes,
    disposition_bytes: bytes,
) -> None:
    """Every file in the package still holds exactly what we wrote."""
    observed = (
        _read_stable_owner_file(package / "payload.bin", _MAX_QUEUE_PAYLOAD_BYTES),
        _read_stable_owner_file(
            package / "attempt-history.json", _MAX_EXPORT_METADATA_BYTES
        ),
        _read_stable_owner_file(
            package / "task-metadata.json", _MAX_EXPORT_METADATA_BYTES
        ),
        _read_stable_owner_file(package / "disposition.json", 64 * 1024),
    )
    if observed != (raw, history, metadata, disposition_bytes):
        raise QueueOperationError("corrupt_export_verification_failed")


def _require_disposable_rows(
    current_task: sqlite3.Row, current_operation: sqlite3.Row, incoming_count: int
) -> None:
    """The task and its operation are in the exact states disposal needs."""
    observed = (
        current_task["state"],
        current_operation["state"],
        current_operation["lineage_generation"],
        current_operation["link_count"],
    )
    expected = (
        "quarantine_pending",
        "manifested",
        current_task["lineage_generation"],
        incoming_count,
    )
    if observed != expected:
        raise QueueOperationError("corrupt_export_verification_failed")


def _blocked_corrupt_progress(
    operation: sqlite3.Row, *, task_id: str, operation_id: str
) -> CorruptExportProgress:
    return CorruptExportProgress(
        task_id=task_id,
        operation_id=operation_id,
        state="blocked",
        pages_written=int(operation["page_count"]),
        links_exported=int(operation["link_count"]),
        complete=False,
        code="capture_link_conflicted",
    )


def _quarantined_corrupt_progress(
    operation: sqlite3.Row, *, task_id: str, operation_id: str
) -> CorruptExportProgress:
    return CorruptExportProgress(
        task_id=task_id,
        operation_id=operation_id,
        state="quarantined",
        pages_written=int(operation["page_count"]),
        links_exported=int(operation["link_count"]),
        complete=True,
        code=None,
    )


def _check_resolution_reason(reason: object) -> None:
    if not isinstance(reason, str) or not 1 <= len(reason.encode("utf-8")) <= 4096:
        raise ValueError("resolution reason is invalid")


def _insert_link_resolution(
    database: sqlite3.Connection,
    *,
    resolution_digest: str,
    task_id: str,
    supersedes_digest: str | None,
    observed_json: bytes,
    selected_id: str | None,
    actor: str,
    reason: str,
    now: datetime,
) -> None:
    inserted = database.execute(
        """INSERT INTO capture_task_link_resolutions(
               resolution_digest,task_id,supersedes_digest,observed_json,
               selected_intent_id,actor_identity,reason,created_at
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (
            resolution_digest,
            task_id,
            supersedes_digest,
            observed_json,
            selected_id,
            actor,
            reason,
            _timestamp(now),
        ),
    ).rowcount
    if inserted != 1:
        raise QueueOperationError("capture_link_resolution_failed")


def _capture_resolution_record(
    *,
    task_id: str,
    supersedes_digest: str | None,
    observed: Mapping[str, object],
    selected_intent: Mapping[str, object] | None,
    actor: str,
    reason: str,
    now: datetime,
) -> dict[str, object]:
    """The schema-valid record one link resolution appends."""
    record = {
        "schema_version": "capture-task-link-resolution/v1",
        "task_id": task_id,
        "supersedes_digest": supersedes_digest,
        "observed": dict(observed),
        "selected_intent": (
            None if selected_intent is None else dict(selected_intent)
        ),
        "actor_identity": actor,
        "reason": reason,
        "created_at": _timestamp(now),
    }
    validate_schema(
        record,
        Path(__file__).with_name("schemas")
        / "capture-task-link-resolution-v1.json",
    )
    return record


def _require_repair_projection(
    database: sqlite3.Connection, owner: OwnerLease, now: datetime
) -> None:
    """The queue still projects this repair owner at this exact epoch."""
    projection = database.execute(
        """SELECT 1 FROM queue_ownership WHERE owner_token=?
           AND fencing_epoch=? AND canonical_role='repair' AND expires_at>?""",
        (owner.token, owner.epoch, _timestamp(now)),
    ).fetchone()
    if projection is None:
        raise QueueOperationError("queue_owner_fence_lost")


def _require_unfenced_unsealed_task(
    database: sqlite3.Connection, task_id: str
) -> None:
    """A fenced or sealed task's links may not be resolved."""
    if database.execute(
        "SELECT 1 FROM task_fences WHERE task_id=?", (task_id,)
    ).fetchone() is not None:
        raise QueueOperationError("task_fenced")
    if database.execute(
        "SELECT 1 FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone() is not None:
        raise QueueOperationError("capture_link_sealed")


def _selected_intent_id(
    database: sqlite3.Connection, selected_intent: Mapping[str, object] | None
) -> str | None:
    """The intent this resolution selects, when it names one that matches."""
    if selected_intent is None:
        return None
    selected_id = selected_intent["intent_id"]
    intent = database.execute(
        "SELECT * FROM capture_intents WHERE intent_id=?", (selected_id,)
    ).fetchone()
    if intent is None or intent["intent_sha256"] != selected_intent["intent_sha256"]:
        raise QueueOperationError("capture_link_conflicted")
    return selected_id


def _read_purge_receipt(package: Path) -> dict[str, object]:
    """The schema-valid purge receipt the package holds."""
    try:
        receipt_bytes = _read_stable_owner_file(
            package / "purge-receipt.json", 64 * 1024
        )
        receipt = json.loads(receipt_bytes.decode("utf-8", errors="strict"))
        validate_schema(
            receipt,
            Path(__file__).with_name("schemas") / "corrupt-purge-v1.json",
        )
    except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        raise QueueOperationError("corrupt_purge_completion_invalid") from exc
    return receipt


def _require_matching_purge_receipt(
    receipt: Mapping[str, object],
    operation: sqlite3.Row,
    disposition: sqlite3.Row,
    task_id: str,
) -> None:
    """The receipt has to name exactly the purge the rows describe."""
    observed = (
        receipt["operation_id"],
        receipt["task_id"],
        receipt["package_key"],
        receipt["manifest_sha256"],
        receipt["disposition_sha256"],
        receipt["purge_page_count"],
        receipt["final_rolling_root"],
        receipt["final_generation"],
    )
    expected = (
        operation["operation_id"],
        task_id,
        disposition["disposition_key"],
        disposition["manifest_sha256"],
        disposition["disposition_sha256"],
        operation["page_count"],
        operation["rolling_root"],
        operation["expected_generation"],
    )
    if observed != expected:
        raise QueueOperationError("corrupt_purge_completion_invalid")


def _require_package_digests(package: Path, disposition: sqlite3.Row) -> None:
    """The manifest and disposition still hash to what the row recorded."""
    manifest_bytes = _read_stable_owner_file(package / "manifest.json", 64 * 1024)
    disposition_bytes = _read_stable_owner_file(
        package / "disposition.json", 64 * 1024
    )
    observed = (sha256_bytes(manifest_bytes), sha256_bytes(disposition_bytes))
    expected = (disposition["manifest_sha256"], disposition["disposition_sha256"])
    if observed != expected:
        raise QueueOperationError("corrupt_package_invalid")


def _purge_receipt_record(
    operation: sqlite3.Row,
    disposition: sqlite3.Row,
    *,
    operation_id: str,
    task_id: str,
) -> dict[str, object]:
    """The schema-valid receipt that closes one purge."""
    receipt = {
        "schema_version": "corrupt-purge/v1",
        "operation_id": operation_id,
        "task_id": task_id,
        "package_key": str(disposition["disposition_key"]),
        "package_path": str(disposition["package_path"]),
        "manifest_sha256": str(disposition["manifest_sha256"]),
        "disposition_sha256": str(disposition["disposition_sha256"]),
        "original_frozen_root": str(disposition["original_frozen_root"]),
        "purge_page_count": int(operation["page_count"]),
        "final_rolling_root": str(operation["rolling_root"]),
        "final_generation": int(operation["expected_generation"]),
        "observed_incoming_link_count": 0,
    }
    validate_schema(
        receipt,
        Path(__file__).with_name("schemas") / "corrupt-purge-v1.json",
    )
    return receipt


def _write_purge_receipt(package: Path, receipt: Mapping[str, object]) -> bytes:
    """Write the receipt and read it back to prove it landed."""
    receipt_bytes = canonical_json_bytes(receipt)
    if len(receipt_bytes) > 64 * 1024:
        raise QueueOperationError("corrupt_purge_receipt_too_large")
    _write_durable_file(package / "purge-receipt.json", receipt_bytes)
    written = _read_stable_owner_file(package / "purge-receipt.json", 64 * 1024)
    if written != receipt_bytes:
        raise QueueOperationError("corrupt_purge_receipt_invalid")
    return receipt_bytes


_CONSUMER_KINDS = frozenset({"transaction", "terminal", "corrupt-disposition"})


def _check_consumer_kind(consumer_kind: str) -> None:
    if consumer_kind not in _CONSUMER_KINDS:
        raise ValueError("consumer kind is invalid")


def _check_consumer_id(consumer_id: object) -> None:
    if not isinstance(consumer_id, str):
        raise ValueError("consumer ID is invalid")
    if not 1 <= len(consumer_id.encode("utf-8")) <= 4096:
        raise ValueError("consumer ID is invalid")


def _capture_seal_digest(
    *,
    task_id: str,
    consumer_kind: str,
    consumer_id: str,
    active_link_digest: str,
) -> str:
    """One digest for exactly this consumer sealing exactly this link."""
    return sha256_bytes(
        canonical_json_bytes(
            {
                "active_digest": active_link_digest,
                "consumer_id": consumer_id,
                "consumer_kind": consumer_kind,
                "task_id": task_id,
            }
        )
    )


def _require_identical_seal(
    existing: sqlite3.Row,
    *,
    consumer_kind: str,
    consumer_id: str,
    active_link_digest: str,
    seal_digest: str,
) -> None:
    """A second seal is a no-op only when it is the very same seal."""
    stored = (
        existing["active_digest"],
        existing["consumer_kind"],
        existing["consumer_id"],
        existing["seal_digest"],
    )
    if stored != (active_link_digest, consumer_kind, consumer_id, seal_digest):
        raise QueueOperationError("capture_link_sealed")


def _record_capture_seal(
    database: sqlite3.Connection,
    *,
    task_id: str,
    consumer_kind: str,
    consumer_id: str,
    active_link_digest: str,
    seal_digest: str,
    now: datetime,
) -> None:
    """Seal the binding, accepting a repeat of the identical seal."""
    existing = database.execute(
        "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone()
    if existing is not None:
        _require_identical_seal(
            existing,
            consumer_kind=consumer_kind,
            consumer_id=consumer_id,
            active_link_digest=active_link_digest,
            seal_digest=seal_digest,
        )
        return
    inserted = database.execute(
        """INSERT INTO capture_task_link_seals(
               task_id,active_digest,consumer_kind,consumer_id,
               seal_digest,sealed_at
           ) VALUES (?,?,?,?,?,?)""",
        (
            task_id,
            active_link_digest,
            consumer_kind,
            consumer_id,
            seal_digest,
            _timestamp(now),
        ),
    ).rowcount
    if inserted != 1:
        raise QueueOperationError("capture_link_seal_failed")


def _descriptor_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_stable_result(descriptor: int) -> bytes | None:
    """The file's bytes, when it neither grew nor moved while we read it."""
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_RESULT_BYTES:
        return None
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        data = handle.read(_MAX_RESULT_BYTES + 1)
    if len(data) > _MAX_RESULT_BYTES:
        return None
    if _descriptor_identity(opened) != _descriptor_identity(os.fstat(descriptor)):
        return None
    return data


def _stable_result_digest(path: Path) -> str | None:
    """The digest of a result file read without following a link or a swap."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        data = _read_stable_result(descriptor)
    finally:
        os.close(descriptor)
    return None if data is None else sha256_bytes(data)


def _bounded_retry_number(value: float) -> float | None:
    """A retry delay in seconds, within its bound; a bad number is no delay."""
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, float(_MAX_RETRY_AFTER_SECONDS))


def _bounded_retry_until(value: datetime, now: datetime) -> float:
    """The wait until a moment, never negative and never past the bound."""
    seconds = max(0.0, (_as_utc(value) - now).total_seconds())
    return min(seconds, float(_MAX_RETRY_AFTER_SECONDS))


def _retry_after_from_text(value: str, now: datetime) -> float | None:
    """A Retry-After header: either a count of seconds or an HTTP date."""
    stripped = value.strip()
    if stripped.isdigit():
        return _bounded_retry_number(float(stripped))
    try:
        parsed = email.utils.parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    return _bounded_retry_until(parsed, now)


class MemoryQueue:
    """Durable priority queue with lease-token fencing and at-least-once delivery."""

    @classmethod
    def _from_v3_candidate(
        cls, path: Path, *, state_root: Path
    ) -> _QueueV3CandidateReader:
        validate_queue_v3_database(path, state_root=state_root)
        return _QueueV3CandidateReader(Path(path))

    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | random.SystemRandom | None = None,
        heartbeat_wait: Callable[[threading.Event, float], bool] | None = None,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        retry_base_seconds: int = DEFAULTS.retry_base_seconds,
        retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
    ) -> None:
        _validate_retry_policy(max_attempts, retry_base_seconds, retry_cap_seconds)
        self.state_root = Path(state_root).resolve()
        self.run_dir = self.state_root / "run"
        self.db_path = self.run_dir / "queue.sqlite3"
        self.results_dir = self.run_dir / "queue-results"
        self._clock = clock or _utc_now
        self._rng = rng or random.SystemRandom()
        self._heartbeat_wait = heartbeat_wait or (
            lambda stop, interval: stop.wait(interval)
        )
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._db_hardened = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.run_dir, 0o700)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.results_dir, 0o700)
        with self._connect() as connection:
            self._create_schema(connection)
            with begin_immediate(connection):
                self._retire_exhausted_ready(
                    connection, _as_utc(self._clock()), self._max_attempts
                )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = open_operational_db(self.db_path, busy_ms=DEFAULTS.queue_busy_ms)
        try:
            # Preserve this legacy context manager's implicit commit/rollback API.
            connection.isolation_level = "DEFERRED"
            if not self._db_hardened:
                _harden_owner_only(self.db_path, 0o600)
                self._db_hardened = True
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        MemoryQueue._upgrade_legacy_attempt_constraint(connection)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                handler_version INTEGER NOT NULL CHECK (handler_version >= 1),
                payload_json TEXT NOT NULL,
                input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
                dedupe_key TEXT UNIQUE,
                state TEXT NOT NULL CHECK (state IN ('ready','leased','blocked','succeeded','dead','cancelled')),
                priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                last_attempt_at TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                lease_heartbeat_at TEXT,
                attempt_started_at TEXT,
                error_code TEXT,
                blocked_capability TEXT,
                result_reference TEXT,
                result_sha256 TEXT,
                result_operation_id TEXT,
                redrive_of TEXT REFERENCES tasks(id)
            );
            CREATE INDEX IF NOT EXISTS queue_claim_order
                ON tasks(state, priority DESC, available_at, created_at);
            CREATE TABLE IF NOT EXISTS source_fences (
                daily_id TEXT PRIMARY KEY,
                source_digest TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL UNIQUE,
                owner_pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_failures (
                logical_path TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                error_code TEXT NOT NULL,
                producer TEXT NOT NULL CHECK (producer IN ('compile','queue')),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (logical_path, source_digest)
            );
            CREATE TABLE IF NOT EXISTS attempt_history (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','blocked','cancelled','lease_expired')),
                error_code TEXT
            );
            CREATE TRIGGER IF NOT EXISTS attempt_history_immutable_update
                BEFORE UPDATE ON attempt_history BEGIN SELECT RAISE(ABORT, 'attempt history is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS attempt_history_immutable_delete
                BEFORE DELETE ON attempt_history BEGIN SELECT RAISE(ABORT, 'attempt history is immutable'); END;
            """
        )
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "last_attempt_at" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN last_attempt_at TEXT")
        if "result_sha256" not in columns:
            connection.execute("ALTER TABLE tasks ADD COLUMN result_sha256 TEXT")
        if "redrive_of" not in columns:
            connection.execute(
                "ALTER TABLE tasks ADD COLUMN redrive_of TEXT REFERENCES tasks(id)"
            )
        source_fence_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(source_fences)").fetchall()
        }
        if "heartbeat_at" not in source_fence_columns:
            connection.execute("ALTER TABLE source_fences ADD COLUMN heartbeat_at TEXT")
            connection.execute(
                "UPDATE source_fences SET heartbeat_at=acquired_at WHERE heartbeat_at IS NULL"
            )
        if "expires_at" not in source_fence_columns:
            connection.execute("ALTER TABLE source_fences ADD COLUMN expires_at TEXT")
            connection.execute(
                "UPDATE source_fences SET expires_at=acquired_at WHERE expires_at IS NULL"
            )

    @staticmethod
    def _upgrade_legacy_attempt_constraint(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if row is None or "attempts BETWEEN 0 AND 8" not in str(row["sql"]):
            return
        old_sql = str(row["sql"])
        new_sql = re.sub(
            r"^CREATE TABLE\s+tasks\b",
            "CREATE TABLE tasks_unbounded",
            old_sql,
            count=1,
            flags=re.IGNORECASE,
        ).replace("attempts BETWEEN 0 AND 8", "attempts >= 0")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with begin_immediate(connection):
                connection.execute(new_sql)
                connection.execute("INSERT INTO tasks_unbounded SELECT * FROM tasks")
                connection.execute("DROP TABLE tasks")
                connection.execute("ALTER TABLE tasks_unbounded RENAME TO tasks")
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    def _retire_exhausted_ready(
        self,
        connection: sqlite3.Connection, now: datetime, max_attempts: int
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='ready' AND attempts >= ?",
            (max_attempts,),
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE tasks SET state='dead', error_code='attempts_exhausted',
                       updated_at=?, last_attempt_at=COALESCE(last_attempt_at, ?)
                   WHERE id=? AND state='ready' AND attempts >= ?""",
                (
                    _timestamp(now),
                    _timestamp(now),
                    row["id"],
                    max_attempts,
                ),
            )
            self._record_payload_failure(
                connection,
                str(row["payload_json"]),
                "attempts_exhausted",
                "queue",
                now,
            )

    def _legacy_deduplicated_task_id(
        self,
        connection: sqlite3.Connection,
        dedupe_key: str | None,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
    ) -> str | None:
        """The id of an identical task already enqueued under this dedupe key."""
        if dedupe_key is None:
            return None
        existing = connection.execute(
            """SELECT id, kind, handler_version, payload_json, input_hash
               FROM tasks WHERE dedupe_key=?""",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            return None
        existing_bytes = _legacy_payload_bytes(existing)
        validation = validate_payload_blob(
            existing_bytes, existing["input_hash"], parse=True
        )
        if validation.code is None and _same_enqueued_task(
            existing, existing_bytes, kind, handler_version, payload_bytes, input_hash
        ):
            return str(existing["id"])
        raise QueueOperationError("dedupe_conflict")

    def _insert_legacy_task_row(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_json: str,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
    ) -> None:
        inserted = connection.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_json, input_hash, dedupe_key,
                   state, priority, created_at, updated_at, available_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)""",
            (
                task_id,
                kind,
                handler_version,
                payload_json,
                input_hash,
                dedupe_key,
                priority,
                _timestamp(now),
                _timestamp(now),
                _timestamp(ready_at),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("enqueue_failed")

    def enqueue(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> str:
        _require_legacy_enqueue_arguments(kind, handler_version, priority, dedupe_key)
        payload_bytes = canonical_json_bytes(_redact_payload(dict(payload)))
        payload_json = payload_bytes.decode("utf-8")
        input_hash = sha256_bytes(payload_bytes)
        now = _as_utc(self._clock())
        ready_at = _as_utc(available_at or now)
        task_id = uuid.UUID(int=self._rng.getrandbits(128)).hex
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            self._assert_payload_not_fenced(connection, payload_json)
            deduplicated = self._legacy_deduplicated_task_id(
                connection, dedupe_key, kind, handler_version, payload_bytes, input_hash
            )
            if deduplicated is not None:
                return deduplicated
            self._insert_legacy_task_row(
                connection,
                task_id,
                kind,
                handler_version,
                payload_json,
                input_hash,
                dedupe_key,
                priority,
                now,
                ready_at,
            )
        return task_id

    def claim(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int | None = None,
    ) -> QueueLease | None:
        if not owner:
            raise ValueError("owner must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        attempt_limit = self._max_attempts if max_attempts is None else max_attempts
        _validate_retry_policy(
            attempt_limit, self._retry_base_seconds, self._retry_cap_seconds
        )
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            self._expire_leases(connection, now, attempt_limit)
            self._delete_stale_source_fences(connection)
            row = connection.execute(
                """SELECT * FROM tasks
                   WHERE state = 'ready' AND attempts < ? AND available_at <= ?
                     AND NOT EXISTS (
                         SELECT 1 FROM source_fences f
                         WHERE instr(tasks.payload_json, f.daily_id) > 0
                            OR instr(tasks.payload_json, f.source_digest) > 0
                     )
                   ORDER BY priority DESC, available_at, created_at, rowid LIMIT 1""",
                (attempt_limit, _timestamp(now)),
            ).fetchone()
            if row is None:
                return None
            token = f"{self._rng.getrandbits(256):064x}"
            expires = now + timedelta(seconds=lease_seconds)
            changed = connection.execute(
                """UPDATE tasks SET state='leased', attempts=attempts+1,
                       lease_owner=?, lease_token=?, lease_expires_at=?, lease_heartbeat_at=?,
                       attempt_started_at=?, updated_at=?, error_code=NULL, blocked_capability=NULL
                   WHERE id=? AND state='ready' AND attempts < ?""",
                (
                    owner,
                    token,
                    _timestamp(expires),
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    row["id"],
                    attempt_limit,
                ),
            ).rowcount
            if changed != 1:
                return None
            attempt = int(row["attempts"]) + 1
        return QueueLease(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=json.loads(row["payload_json"]),
            input_hash=str(row["input_hash"]),
            owner=owner,
            token=token,
            expires_at=expires,
            attempt=attempt,
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    def _claim_task(self, task_id: str, owner: str) -> QueueLease | None:
        """Claim one known task for the legacy mark_attempt facade."""
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=? AND state='ready'", (task_id,)
            ).fetchone()
            if row is None:
                return None
            try:
                self._assert_payload_not_fenced(connection, str(row["payload_json"]))
            except QueueOperationError:
                return None
            token = f"{self._rng.getrandbits(256):064x}"
            expires = now + timedelta(seconds=DEFAULTS.queue_lease_seconds)
            connection.execute(
                """UPDATE tasks SET state='leased', attempts=attempts+1, lease_owner=?,
                       lease_token=?, lease_expires_at=?, lease_heartbeat_at=?,
                       attempt_started_at=?, updated_at=? WHERE id=?""",
                (
                    owner,
                    token,
                    _timestamp(expires),
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    task_id,
                ),
            )
        return QueueLease(
            task_id,
            row["kind"],
            row["handler_version"],
            json.loads(row["payload_json"]),
            row["input_hash"],
            owner,
            token,
            expires,
            int(row["attempts"]) + 1,
            _parse_timestamp(row["created_at"]),
            _parse_timestamp(row["last_attempt_at"]),
            int(row["attempts"]),
        )

    def _expire_leases(
        self, connection: sqlite3.Connection, now: datetime, max_attempts: int
    ) -> int:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='leased' AND lease_expires_at <= ?",
            (_timestamp(now),),
        ).fetchall()
        for row in rows:
            if row["result_reference"] is not None or row["result_sha256"] is not None:
                valid = self._stored_result_is_valid(row)
                error_code = None if valid else "result_corrupt"
                self._record_attempt(
                    connection,
                    row,
                    now,
                    "succeeded" if valid else "failed",
                    error_code,
                )
                self._finish_lease(
                    connection,
                    row["id"],
                    now,
                    "succeeded" if valid else "dead",
                    error_code,
                    None,
                    last_attempt_at=now,
                )
                continue
            exhausted = int(row["attempts"]) >= max_attempts
            error_code = "attempts_exhausted" if exhausted else "lease_expired"
            state = "dead" if exhausted else "ready"
            self._record_attempt(connection, row, now, "lease_expired", error_code)
            connection.execute(
                """UPDATE tasks SET state=?, available_at=?, updated_at=?, last_attempt_at=?,
                       lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                       lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?
                   WHERE id=? AND state='leased' AND lease_token=?""",
                (
                    state,
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    error_code,
                    row["id"],
                    row["lease_token"],
                ),
            )
        return len(rows)

    def _stored_result_is_valid(self, row: sqlite3.Row) -> bool:
        reference = row["result_reference"]
        digest = row["result_sha256"]
        if not isinstance(reference, str) or not isinstance(digest, str):
            return False
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False
        return self._validated_result_digest(reference) == digest

    def _result_path_usable(self, path: Path) -> bool:
        """The reference names a real, owner-only file directly in results."""
        resolved = path.resolve(strict=True)
        resolved.relative_to(self.results_dir.resolve())
        if path.parent.resolve() != self.results_dir.resolve() or path.is_symlink():
            return False
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            return False
        if metadata.st_size > _MAX_RESULT_BYTES:
            return False
        return _is_owner_only(path)

    def _validated_result_digest(self, reference: str) -> str | None:
        try:
            path = self.state_root / reference
            if not self._result_path_usable(path):
                return None
            return _stable_result_digest(path)
        except (OSError, ValueError):
            return None

    def _read_result_for_export(self, reference: str, digest: str) -> bytes:
        try:
            path = self.state_root / reference
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.results_dir.resolve())
            if path.parent.resolve() != self.results_dir.resolve():
                raise QueueOperationError("result_verification_failed")
            data = _read_stable_owner_file(path, _MAX_RESULT_BYTES)
            if sha256_bytes(data) != digest:
                raise QueueOperationError("result_verification_failed")
            return data
        except QueueOperationError:
            raise
        except (OSError, ValueError):
            raise QueueOperationError("result_verification_failed") from None

    def adopt_published_result(
        self, lease: QueueLease, *, operation_id: str
    ) -> str | None:
        """Adopt an owner-only stable result left before its DB reference committed."""
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        if not target.exists() and not target.is_symlink():
            return None
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            digest = self._validated_result_digest(relative)
            if digest is None:
                self._record_attempt(
                    connection, row, now, "failed", "result_corrupt"
                )
                self._finish_lease(
                    connection,
                    lease.id,
                    now,
                    "dead",
                    "result_corrupt",
                    None,
                    last_attempt_at=now,
                )
                return "corrupt"
            connection.execute(
                """UPDATE tasks SET result_reference=?, result_sha256=?,
                       result_operation_id=?, updated_at=? WHERE id=?""",
                (relative, digest, operation_id, _timestamp(now), lease.id),
            )
            return "adopted"

    def heartbeat(
        self,
        lease: QueueLease,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> QueueLease:
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        now = _as_utc(self._clock())
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._require_lease(connection, lease, now)
            connection.execute(
                "UPDATE tasks SET lease_expires_at=?, lease_heartbeat_at=?, updated_at=? WHERE id=?",
                (_timestamp(expires), _timestamp(now), _timestamp(now), lease.id),
            )
        return replace(lease, expires_at=expires)

    def _fail_corrupt_result(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, object],
        lease: QueueLease,
        now: datetime,
    ) -> None:
        """A result we cannot read again kills the lease that published it."""
        self._record_attempt(connection, row, now, "failed", "result_corrupt")
        self._finish_lease(
            connection,
            lease.id,
            now,
            "dead",
            "result_corrupt",
            None,
            last_attempt_at=now,
        )

    def _existing_result_conflict(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, object],
        lease: QueueLease,
        now: datetime,
        relative: str,
        digest: str,
    ) -> ResultConflictError | None:
        """Why the result already in place cannot stand for this publication."""
        existing_digest = self._validated_result_digest(relative)
        if existing_digest is None:
            self._fail_corrupt_result(connection, row, lease, now)
            return ResultConflictError("result_corrupt")
        if existing_digest != digest:
            return ResultConflictError(
                "operation ID already has different result bytes"
            )
        return None

    def _record_published_result(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        now: datetime,
        relative: str,
        digest: str,
        operation_id: str,
        target: Path,
        linked: bool,
    ) -> None:
        """Make the published bytes, and the row that names them, durable."""
        if linked:
            _harden_owner_only(target, 0o600)
            fsync_file(target)
        fsync_directory(self.results_dir)
        connection.execute(
            """UPDATE tasks SET result_reference=?, result_sha256=?,
                   result_operation_id=?, updated_at=?
               WHERE id=?""",
            (relative, digest, operation_id, _timestamp(now), lease.id),
        )

    def _commit_result(
        self,
        lease: QueueLease,
        operation_id: str,
        digest: str,
        relative: str,
        target: Path,
        temporary: Path,
    ) -> ResultConflictError | None:
        """Publish the staged bytes under the lease, or say why it cannot be."""
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            _check_result_operation(row, operation_id)
            linked = _link_result(temporary, target)
            if not linked:
                conflict = self._existing_result_conflict(
                    connection, row, lease, now, relative, digest
                )
                if conflict is not None:
                    return conflict
            self._record_published_result(
                connection, lease, now, relative, digest, operation_id, target, linked
            )
        return None

    def publish_result(
        self, lease: QueueLease, *, operation_id: str, result: bytes
    ) -> str:
        _check_result_request(operation_id, result)
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        temporary = self.results_dir / f".{result_name}.{uuid.uuid4().hex}.tmp"
        try:
            _write_result_file(temporary, result)
            conflict = self._commit_result(
                lease,
                operation_id,
                sha256_bytes(result),
                relative,
                target,
                temporary,
            )
        finally:
            _discard_temporary(temporary)
        if conflict is not None:
            raise conflict
        return relative

    def acknowledge(self, lease: QueueLease) -> QueueTask:
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if not self._stored_result_is_valid(row):
                self._record_attempt(
                    connection, row, now, "failed", "result_corrupt"
                )
                self._finish_lease(
                    connection,
                    lease.id,
                    now,
                    "dead",
                    "result_corrupt",
                    None,
                    last_attempt_at=now,
                )
            else:
                self._record_attempt(connection, row, now, "succeeded", None)
                self._finish_lease(
                    connection, lease.id, now, "succeeded", None, None
                )
        return self.get(lease.id)

    def fail(
        self,
        lease: QueueLease,
        failure: QueueFailure,
        *,
        max_attempts: int | None = None,
        retry_base_seconds: int | None = None,
        retry_cap_seconds: int | None = None,
    ) -> None:
        if not failure.error_code:
            raise ValueError("error_code must be non-empty")
        attempt_limit = self._max_attempts if max_attempts is None else max_attempts
        retry_base = (
            self._retry_base_seconds
            if retry_base_seconds is None
            else retry_base_seconds
        )
        retry_cap = (
            self._retry_cap_seconds if retry_cap_seconds is None else retry_cap_seconds
        )
        _validate_retry_policy(attempt_limit, retry_base, retry_cap)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if failure.blocked_capability:
                self._record_attempt(connection, row, now, "blocked", failure.error_code)
                self._record_payload_failure(
                    connection,
                    str(row["payload_json"]),
                    failure.error_code,
                    "queue",
                    now,
                )
                connection.execute(
                    """UPDATE tasks SET state='blocked', attempts=attempts-1, updated_at=?,
                           lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                           lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?,
                           blocked_capability=? WHERE id=?""",
                    (
                        _timestamp(now),
                        failure.error_code,
                        failure.blocked_capability,
                        lease.id,
                    ),
                )
                return
            dead = (
                failure.permanent
                or failure.error_code in _PERMANENT_CODES
                or int(row["attempts"]) >= attempt_limit
            )
            self._record_attempt(connection, row, now, "failed", failure.error_code)
            if dead:
                self._finish_lease(
                    connection,
                    lease.id,
                    now,
                    "dead",
                    failure.error_code,
                    None,
                    last_attempt_at=now,
                )
                return
            delay = self._retry_delay(
                int(row["attempts"]),
                retry_base_seconds=retry_base,
                retry_cap_seconds=retry_cap,
            )
            retry_after = self._retry_after_seconds(failure.retry_after, now)
            if retry_after is not None:
                delay = max(delay, retry_after)
            self._finish_lease(
                connection,
                lease.id,
                now,
                "ready",
                failure.error_code,
                now + timedelta(seconds=delay),
                last_attempt_at=now,
            )

    def _retry_delay(
        self,
        attempts: int,
        *,
        retry_base_seconds: int | None = None,
        retry_cap_seconds: int | None = None,
    ) -> float:
        retry_base = (
            self._retry_base_seconds
            if retry_base_seconds is None
            else retry_base_seconds
        )
        retry_cap = (
            self._retry_cap_seconds if retry_cap_seconds is None else retry_cap_seconds
        )
        jitter_cap = min(
            retry_cap,
            retry_base * (2 ** (attempts - 1)),
        )
        return self._rng.uniform(0, jitter_cap)

    @staticmethod
    def _retry_after_seconds(
        value: float | datetime | str | None, now: datetime
    ) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return _bounded_retry_number(float(value))
        if isinstance(value, datetime):
            return _bounded_retry_until(value, now)
        if isinstance(value, str):
            return _retry_after_from_text(value, now)
        return None

    @staticmethod
    def _record_attempt(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        outcome: str,
        error_code: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO attempt_history(
                   task_id, attempt, started_at, finished_at, outcome, error_code
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["attempts"],
                row["attempt_started_at"] or _timestamp(now),
                _timestamp(now),
                outcome,
                error_code,
            ),
        )

    def _finish_lease(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        now: datetime,
        state: str,
        error_code: str | None,
        available_at: datetime | None,
        *,
        last_attempt_at: datetime | None = None,
    ) -> None:
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is not None:
            payload_json = str(row["payload_json"])
            if error_code is not None and state in {"ready", "dead"}:
                self._record_payload_failure(
                    connection, payload_json, error_code, "queue", now
                )
            elif state in {"succeeded", "cancelled"}:
                self._clear_payload_failure(connection, payload_json)
        connection.execute(
            """UPDATE tasks SET state=?, updated_at=?, available_at=COALESCE(?, available_at),
                   last_attempt_at=COALESCE(?, last_attempt_at),
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?,
                   blocked_capability=NULL WHERE id=?""",
            (
                state,
                _timestamp(now),
                _timestamp(available_at) if available_at else None,
                _timestamp(last_attempt_at) if last_attempt_at else None,
                error_code,
                task_id,
            ),
        )

    @staticmethod
    def _require_lease(
        connection: sqlite3.Connection, lease: QueueLease, now: datetime
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM tasks WHERE id=?", (lease.id,)).fetchone()
        if (
            row is None
            or row["state"] != "leased"
            or row["lease_owner"] != lease.owner
            or row["lease_token"] != lease.token
            or row["lease_expires_at"] <= _timestamp(now)
        ):
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        return row

    def cancel(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        _require_active(deadline, cancelled)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in _TERMINAL_STATES:
                return False
            if row["state"] == "leased":
                self._record_attempt(connection, row, now, "cancelled", "cancelled")
            self._clear_payload_failure(connection, str(row["payload_json"]))
            _require_active(deadline, cancelled)
            connection.execute(
                """UPDATE tasks SET state='cancelled', updated_at=?, lease_owner=NULL,
                       lease_token=NULL, lease_expires_at=NULL, lease_heartbeat_at=NULL,
                       attempt_started_at=NULL, error_code='cancelled' WHERE id=?""",
                (_timestamp(now), task_id),
            )
            return True

    def redrive(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        _require_active(deadline, cancelled)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] != "dead":
                raise QueueOperationError("redrive_requires_dead")
            self._delete_stale_source_fences(connection)
            self._assert_payload_not_fenced(connection, str(row["payload_json"]))
            payload_bytes = str(row["payload_json"]).encode("utf-8")
            replacement = uuid.UUID(int=self._rng.getrandbits(128)).hex
            _require_active(deadline, cancelled)
            connection.execute(
                """INSERT INTO tasks(
                       id, kind, handler_version, payload_json, input_hash, state,
                       priority, created_at, updated_at, available_at, redrive_of
                   ) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
                (
                    replacement,
                    row["kind"],
                    row["handler_version"],
                    payload_bytes.decode("utf-8"),
                    sha256_bytes(payload_bytes),
                    row["priority"],
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    task_id,
                ),
            )
        return replacement

    @staticmethod
    def _validate_source_identity(daily_id: str, source_digest: str) -> None:
        if not isinstance(daily_id, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", daily_id) is None:
            raise ValueError("daily_id must be a canonical date")
        try:
            if date.fromisoformat(daily_id).isoformat() != daily_id:
                raise ValueError
        except ValueError as exc:
            raise ValueError("daily_id must be a canonical date") from exc
        if not isinstance(source_digest, str) or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None:
            raise ValueError("source_digest must be a lowercase SHA-256")

    @staticmethod
    def _validate_failure_identity(logical_path: str, source_digest: str) -> None:
        if (
            not isinstance(logical_path, str)
            or re.fullmatch(r"knowledge/daily/\d{4}-\d{2}-\d{2}\.md", logical_path)
            is None
        ):
            raise ValueError("logical_path must name a canonical daily source")
        MemoryQueue._validate_source_identity(
            Path(logical_path).stem, source_digest
        )

    @staticmethod
    def _source_identity_from_payload(payload_json: str) -> tuple[str, str] | None:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            return None
        paths: list[str] = []
        digests: list[str] = []
        daily_ids: list[str] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    normalized = str(key).casefold()
                    if isinstance(item, str):
                        if normalized in {"source_path", "logical_path"}:
                            paths.append(item)
                        elif normalized in {"source_digest", "digest", "hash"}:
                            digests.append(item)
                        elif normalized == "daily_id":
                            daily_ids.append(item)
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(payload)
        if not paths and daily_ids:
            paths.append(f"knowledge/daily/{daily_ids[0]}.md")
        if not paths or not digests:
            return None
        try:
            MemoryQueue._validate_failure_identity(paths[0], digests[0])
        except ValueError:
            return None
        return paths[0], digests[0]

    @staticmethod
    def _record_source_failure_row(
        connection: sqlite3.Connection,
        logical_path: str,
        source_digest: str,
        error_code: str,
        producer: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO source_failures(
                   logical_path, source_digest, error_code, producer, updated_at
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(logical_path, source_digest) DO UPDATE SET
                   error_code=excluded.error_code,
                   producer=excluded.producer,
                   updated_at=excluded.updated_at""",
            (logical_path, source_digest, error_code, producer, _timestamp(now)),
        )

    def _record_payload_failure(
        self,
        connection: sqlite3.Connection,
        payload_json: str,
        error_code: str,
        producer: str,
        now: datetime,
    ) -> None:
        identity = self._source_identity_from_payload(payload_json)
        if identity is not None:
            self._record_source_failure_row(
                connection, *identity, error_code, producer, now
            )

    def _clear_payload_failure(
        self, connection: sqlite3.Connection, payload_json: str
    ) -> None:
        identity = self._source_identity_from_payload(payload_json)
        if identity is not None:
            connection.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                identity,
            )

    def record_source_failure(
        self,
        logical_path: str,
        source_digest: str,
        *,
        error_code: str,
        producer: str,
    ) -> None:
        self._validate_failure_identity(logical_path, source_digest)
        if (
            not isinstance(error_code, str)
            or not 1 <= len(error_code) <= 200
            or any(char in error_code for char in "\r\n")
            or producer not in {"compile", "queue"}
        ):
            raise ValueError("source failure fields are invalid")
        with self._connect() as connection, begin_immediate(connection):
            self._record_source_failure_row(
                connection,
                logical_path,
                source_digest,
                error_code,
                producer,
                _as_utc(self._clock()),
            )

    def clear_source_failure(self, logical_path: str, source_digest: str) -> None:
        self._validate_failure_identity(logical_path, source_digest)
        with self._connect() as connection, begin_immediate(connection):
            connection.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            )

    def source_failure(
        self, logical_path: str, source_digest: str
    ) -> dict[str, str] | None:
        self._validate_failure_identity(logical_path, source_digest)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT logical_path, source_digest, error_code, producer "
                "FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            ).fetchone()
        return None if row is None else {key: str(row[key]) for key in row.keys()}

    @staticmethod
    def _payload_references_source(
        payload_json: str, daily_id: str, source_digest: str
    ) -> bool:
        return daily_id in payload_json or source_digest in payload_json

    def _delete_stale_source_fences(self, connection: sqlite3.Connection) -> None:
        now = _as_utc(self._clock())
        rows = connection.execute(
            "SELECT token, owner_pid, expires_at FROM source_fences"
        ).fetchall()
        for row in rows:
            expires_at = _parse_timestamp(str(row["expires_at"]))
            expired = expires_at is None or expires_at <= now
            if expired or not _pid_is_alive(int(row["owner_pid"])):
                connection.execute(
                    "DELETE FROM source_fences WHERE token=?", (row["token"],)
                )

    def _assert_payload_not_fenced(
        self, connection: sqlite3.Connection, payload_json: str
    ) -> None:
        for row in connection.execute(
            "SELECT daily_id, source_digest FROM source_fences"
        ):
            if self._payload_references_source(
                payload_json, str(row["daily_id"]), str(row["source_digest"])
            ):
                raise QueueOperationError("source_fenced")

    def referencing_source_tasks(
        self,
        daily_id: str,
        source_digest: str,
        *,
        states: tuple[str, ...] = ("ready", "leased", "blocked", "dead"),
    ) -> tuple[str, ...]:
        self._validate_source_identity(daily_id, source_digest)
        if not states or any(state not in _STATES for state in states):
            raise ValueError("invalid queue state")
        placeholders = ",".join("?" for _ in states)
        with self._connect() as connection:
            cursor = connection.execute(
                f"SELECT id, payload_json FROM tasks WHERE state IN ({placeholders}) "
                "ORDER BY created_at, rowid",  # noqa: S608 - generated placeholders
                states,
            )
            matches = []
            for row in cursor:
                if self._payload_references_source(
                    str(row["payload_json"]), daily_id, source_digest
                ):
                    matches.append(str(row["id"]))
            return tuple(matches)

    def acquire_source_fence(
        self,
        daily_id: str,
        source_digest: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        self._validate_source_identity(daily_id, source_digest)
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("source fence lease must be a positive integer")
        token = f"{self._rng.getrandbits(256):064x}"
        acquired = _as_utc(self._clock())
        expires = acquired + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            for row in connection.execute(
                "SELECT id, payload_json FROM tasks "
                "WHERE state IN ('ready','leased','blocked','dead')"
            ):
                if self._payload_references_source(
                    str(row["payload_json"]), daily_id, source_digest
                ):
                    raise QueueOperationError("source_referenced")
            try:
                connection.execute(
                    "INSERT INTO source_fences "
                    "(daily_id, source_digest, token, owner_pid, acquired_at, "
                    "heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        daily_id,
                        source_digest,
                        token,
                        os.getpid(),
                        _timestamp(acquired),
                        _timestamp(acquired),
                        _timestamp(expires),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise QueueOperationError("source_fenced") from exc
        return SourceFence(
            daily_id,
            source_digest,
            token,
            os.getpid(),
            acquired,
            acquired,
            expires,
        )

    def heartbeat_source_fence(
        self,
        fence: SourceFence,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        if not isinstance(fence, SourceFence):
            raise TypeError("source heartbeat requires a SourceFence")
        if not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or lease_seconds <= 0:
            raise ValueError("source fence lease must be a positive integer")
        now = _as_utc(self._clock())
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            changed = connection.execute(
                """UPDATE source_fences SET heartbeat_at=?, expires_at=?
                   WHERE daily_id=? AND source_digest=? AND token=? AND owner_pid=?
                     AND acquired_at=? AND heartbeat_at=? AND expires_at=?""",
                (
                    _timestamp(now),
                    _timestamp(expires),
                    fence.daily_id,
                    fence.source_digest,
                    fence.token,
                    fence.owner_pid,
                    _timestamp(fence.acquired_at),
                    _timestamp(fence.heartbeat_at),
                    _timestamp(fence.expires_at),
                ),
            ).rowcount
            if changed != 1 or now >= fence.expires_at:
                raise QueueOperationError("source_fence_lost")
        return replace(fence, heartbeat_at=now, expires_at=expires)

    @contextmanager
    def source_fence_heartbeat(
        self,
        fence: SourceFence,
        *,
        heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ):
        if (
            not isinstance(heartbeat_seconds, int)
            or isinstance(heartbeat_seconds, bool)
            or heartbeat_seconds <= 0
            or not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds <= heartbeat_seconds
        ):
            raise ValueError("source heartbeat interval must be shorter than its lease")
        heartbeat = _SourceFenceHeartbeat(
            self,
            fence,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
        )
        heartbeat.start()
        try:
            yield heartbeat
        finally:
            heartbeat.stop()

    @contextmanager
    def source_finalization(self, fence: SourceFence):
        if not isinstance(fence, SourceFence):
            raise TypeError("source finalization requires a SourceFence")
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            self._require_live_source_fence(connection, fence)
            self._require_no_source_failure(connection, fence)
            self._require_no_source_reference(connection, fence)
            yield

    def _require_live_source_fence(
        self, connection: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """The fence row still matches this holder, and has not expired."""
        row = connection.execute(
            "SELECT daily_id, source_digest, token, owner_pid, acquired_at, expires_at "
            "FROM source_fences WHERE token=?",
            (fence.token,),
        ).fetchone()
        if row is None or not _source_fence_row_matches(row, fence):
            raise QueueOperationError("source_fence_lost")
        expires_at = _parse_timestamp(str(row["expires_at"]))
        if expires_at is None or _as_utc(self._clock()) >= expires_at:
            raise QueueOperationError("source_fence_lost")

    @staticmethod
    def _require_no_source_failure(
        connection: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """A source that already failed must not be finalized."""
        logical_path = f"knowledge/daily/{fence.daily_id}.md"
        row = connection.execute(
            "SELECT 1 FROM source_failures "
            "WHERE logical_path=? AND source_digest=?",
            (logical_path, fence.source_digest),
        ).fetchone()
        if row is not None:
            raise QueueOperationError("source_failure")

    def _require_no_source_reference(
        self, connection: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """No retained task may still point at the source being finalized."""
        for task in connection.execute(
            "SELECT payload_json FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        ):
            if self._payload_references_source(
                str(task["payload_json"]), fence.daily_id, fence.source_digest
            ):
                raise QueueOperationError("source_referenced")

    def release_source_fence(self, token: str) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("source fence token is invalid")
        with self._connect() as connection, begin_immediate(connection):
            changed = connection.execute(
                "DELETE FROM source_fences WHERE token=? AND owner_pid=?",
                (token, os.getpid()),
            ).rowcount
            if changed != 1:
                raise QueueOperationError("source_fence_lost")

    def retains_run_directory(self) -> bool:
        with self._connect() as connection:
            task = connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
            source_fence = connection.execute(
                "SELECT 1 FROM source_fences LIMIT 1"
            ).fetchone()
            source_failure = connection.execute(
                "SELECT 1 FROM source_failures LIMIT 1"
            ).fetchone()
        if task is not None or source_fence is not None or source_failure is not None:
            return True
        quarantine = self.run_dir / "queue-quarantine"
        try:
            if quarantine.exists() and any(quarantine.iterdir()):
                return True
        except OSError:
            return True
        try:
            return any(self.results_dir.iterdir())
        except OSError:
            return True

    def _purge_selection(
        self, states: tuple[str, ...], cutoff: datetime
    ) -> tuple[list, dict[str, list]]:
        with self._connect() as connection:
            state_places = ",".join("?" for _ in states)
            rows = connection.execute(
                f"""SELECT * FROM tasks
                    WHERE state IN ({state_places}) AND updated_at < ?
                    ORDER BY created_at, rowid""",  # noqa: S608
                (*states, _timestamp(cutoff)),
            ).fetchall()
            histories = {
                str(row["id"]): connection.execute(
                    "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence",
                    (row["id"],),
                ).fetchall()
                for row in rows
            }
        return rows, histories

    def _export_results(
        self,
        rows: list,
        results_export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        result_manifest: list[dict[str, str]] = []
        for row in rows:
            _require_active(deadline, cancelled)
            reference = row["result_reference"]
            if reference is None:
                continue
            digest = row["result_sha256"]
            if not isinstance(digest, str):
                raise QueueOperationError("result_verification_failed")
            result = self._read_result_for_export(str(reference), digest)
            _write_durable_file(results_export / f"{row['id']}.result", result)
            result_manifest.append({"id": str(row["id"]), "sha256": digest})
        return result_manifest

    def _verify_export(
        self,
        staging: Path,
        results_export: Path,
        records_bytes: bytes,
        manifest_bytes: bytes,
        result_manifest: list[dict[str, str]],
    ) -> None:
        """Read every exported byte back before anything is deleted."""
        records = _read_stable_owner_file(
            staging / "records.json", _MAX_EXPORT_METADATA_BYTES
        )
        if records != records_bytes:
            raise QueueOperationError("export_verification_failed")
        for item in result_manifest:
            exported = results_export / f"{item['id']}.result"
            data = _read_stable_owner_file(exported, _MAX_RESULT_BYTES)
            if sha256_bytes(data) != item["sha256"]:
                raise QueueOperationError("export_verification_failed")
        manifest = _read_stable_owner_file(
            staging / "manifest.json", _MAX_EXPORT_METADATA_BYTES
        )
        if manifest != manifest_bytes:
            raise QueueOperationError("export_verification_failed")

    def _write_export_package(
        self,
        rows: list,
        records_bytes: bytes,
        task_ids: tuple[str, ...],
        staging: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        results_export = staging / "results"
        results_export.mkdir()
        _harden_owner_only(results_export, 0o700)
        result_manifest = self._export_results(
            rows, results_export, deadline, cancelled
        )
        _write_durable_file(staging / "records.json", records_bytes)
        manifest_bytes = canonical_json_bytes(
            {
                "records_sha256": sha256_bytes(records_bytes),
                "results": result_manifest,
                "task_ids": list(task_ids),
            }
        )
        _write_durable_file(staging / "manifest.json", manifest_bytes)
        fsync_directory(results_export)
        fsync_directory(staging)
        self._verify_export(
            staging, results_export, records_bytes, manifest_bytes, result_manifest
        )

    def _publish_export(
        self,
        rows: list,
        records_bytes: bytes,
        task_ids: tuple[str, ...],
        export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        parent = export.parent
        _require_export_parent(parent)
        _cleanup_export_staging(parent, export.name)
        staging = parent / f".{export.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        _harden_owner_only(staging, 0o700)
        try:
            self._write_export_package(
                rows, records_bytes, task_ids, staging, deadline, cancelled
            )
            _require_active(deadline, cancelled)
            staging.replace(export)
            fsync_directory(parent)
        except Exception:
            _remove_export_staging(staging)
            raise

    def _delete_purged_tasks(
        self,
        task_ids: tuple[str, ...],
        states: tuple[str, ...],
        cutoff: datetime,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        _require_active(deadline, cancelled)
        placeholders = ",".join("?" for _ in task_ids)
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            current = connection.execute(
                f"""SELECT id, state, updated_at FROM tasks
                    WHERE id IN ({placeholders})""",  # noqa: S608
                task_ids,
            ).fetchall()
            if _purge_selection_changed(
                current, task_ids, states, _timestamp(cutoff)
            ):
                raise QueueOperationError("purge_selection_changed")
            _require_active(deadline, cancelled)
            connection.execute("DROP TRIGGER attempt_history_immutable_delete")
            connection.execute(
                f"DELETE FROM attempt_history WHERE task_id IN ({placeholders})",  # noqa: S608
                task_ids,
            )
            connection.execute(
                f"DELETE FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
                task_ids,
            )
            connection.execute(
                """CREATE TRIGGER attempt_history_immutable_delete
                   BEFORE DELETE ON attempt_history BEGIN
                   SELECT RAISE(ABORT, 'attempt history is immutable'); END"""
            )

    def _drop_unreferenced_results(
        self, rows: list, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        for row in rows:
            _require_active(deadline, cancelled)
            reference = row["result_reference"]
            if reference is None:
                continue
            with self._connect() as connection:
                retained = connection.execute(
                    "SELECT 1 FROM tasks WHERE result_reference=? LIMIT 1",
                    (reference,),
                ).fetchone()
            if retained is None:
                (self.state_root / str(reference)).unlink(missing_ok=True)
        fsync_directory(self.results_dir)

    def _retire_purged_tasks(
        self,
        rows: list,
        task_ids: tuple[str, ...],
        states: tuple[str, ...],
        cutoff: datetime,
        export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        try:
            self._delete_purged_tasks(task_ids, states, cutoff, deadline, cancelled)
        except BaseException:
            _remove_export_staging(export)
            raise
        self._drop_unreferenced_results(rows, deadline, cancelled)

    def purge(
        self,
        *,
        terminal_before: datetime,
        export_path: Path,
        include_dead: bool = False,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> PurgeReceipt:
        """Export, verify, then delete terminal tasks older than the cutoff.

        `include_dead` extends the selection to attempts-exhausted tasks. It is
        off by default: a dead task is evidence that work never happened, so
        retiring it stays an explicit operator action.
        """
        _require_active(deadline, cancelled)
        states = _purge_states(include_dead)
        retention_cutoff = _as_utc(self._clock()) - timedelta(
            days=DEFAULTS.queue_result_retention_days
        )
        cutoff = min(_as_utc(terminal_before), retention_cutoff)
        export = Path(export_path).absolute()
        if export.exists():
            raise QueueOperationError("export_exists")
        rows, histories = self._purge_selection(states, cutoff)
        task_ids = tuple(str(row["id"]) for row in rows)
        records_bytes = canonical_json_bytes(
            [
                _export_task_record(
                    self._task_from_row(row, histories[str(row["id"])])
                )
                for row in rows
            ]
        )
        self._publish_export(
            rows, records_bytes, task_ids, export, deadline, cancelled
        )
        if task_ids:
            self._retire_purged_tasks(
                rows, task_ids, states, cutoff, export, deadline, cancelled
            )
        return PurgeReceipt(len(task_ids), task_ids)

    def restore(
        self,
        *,
        export_path: Path,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> RestoreReceipt:
        """Re-enqueue the work in one verified purge export.

        A purged task leaves the queue with its history, so restoring it means
        the work runs again: each record returns as a new ready task and the
        receipt maps the exported id to the new one. Nothing is restored unless
        the manifest and every digest verify first.
        """
        _require_active(deadline, cancelled)
        records = _verified_export_records(Path(export_path).absolute())
        restored: list[tuple[str, str]] = []
        for record in records:
            _require_active(deadline, cancelled)
            restored.append((str(record["id"]), self._restore_one(record)))
        return RestoreReceipt(len(restored), tuple(restored))

    def _restore_one(self, record: Mapping[str, object]) -> str:
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            raise QueueOperationError("restore_record_invalid", "payload is not an object")
        return self.enqueue(
            str(record.get("kind") or ""),
            _as_positive_int(record.get("handler_version"), "handler_version"),
            payload,
            priority=_as_priority(record.get("priority")),
        )

    def recover_expired_leases(self) -> int:
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            return self._expire_leases(connection, now, self._max_attempts)

    def get(self, task_id: str) -> QueueTask:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            history = connection.execute(
                "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence", (task_id,)
            ).fetchall()
        return self._task_from_row(row, history)

    def list_tasks(
        self, *, states: tuple[str, ...] | None = None, max_age_days: int | None = None
    ) -> list[QueueTask]:
        states = states or _STATES
        if not states or any(state not in _STATES for state in states):
            raise ValueError("invalid queue state")
        placeholders = ",".join("?" for _ in states)
        parameters: list[object] = list(states)
        age_clause = ""
        if max_age_days is not None:
            cutoff = _as_utc(self._clock()) - timedelta(days=max_age_days)
            age_clause = " AND created_at >= ?"
            parameters.append(_timestamp(cutoff))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM tasks WHERE state IN ({placeholders}){age_clause}
                    ORDER BY created_at, rowid""",  # noqa: S608 - generated placeholders
                parameters,
            ).fetchall()
            histories: dict[str, list[sqlite3.Row]] = {}
            if rows:
                ids = [row["id"] for row in rows]
                id_placeholders = ",".join("?" for _ in ids)
                for history in connection.execute(
                    f"""SELECT * FROM attempt_history WHERE task_id IN ({id_placeholders})
                        ORDER BY sequence""",  # noqa: S608
                    ids,
                ):
                    histories.setdefault(str(history["task_id"]), []).append(history)
        return [self._task_from_row(row, histories.get(str(row["id"]), [])) for row in rows]

    def count_eligible(self, *, max_attempts: int = DEFAULTS.queue_max_attempts) -> int:
        _validate_retry_policy(
            max_attempts, self._retry_base_seconds, self._retry_cap_seconds
        )
        now = _as_utc(self._clock())
        with self._connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) FROM tasks
                   WHERE state='ready' AND attempts < ? AND available_at <= ?""",
                (max_attempts, _timestamp(now)),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _task_from_row(row: sqlite3.Row, history: list[sqlite3.Row]) -> QueueTask:
        return QueueTask(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=json.loads(row["payload_json"]),
            input_hash=str(row["input_hash"]),
            dedupe_key=row["dedupe_key"],
            state=str(row["state"]),
            priority=int(row["priority"]),
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            updated_at=_parse_timestamp(row["updated_at"]),  # type: ignore[arg-type]
            available_at=_parse_timestamp(row["available_at"]),  # type: ignore[arg-type]
            attempts=int(row["attempts"]),
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            lease_owner=row["lease_owner"],
            lease_token=row["lease_token"],
            lease_expires_at=_parse_timestamp(row["lease_expires_at"]),
            lease_heartbeat_at=_parse_timestamp(row["lease_heartbeat_at"]),
            error_code=row["error_code"],
            blocked_capability=row["blocked_capability"],
            result_reference=row["result_reference"],
            result_sha256=row["result_sha256"],
            redrive_of=row["redrive_of"],
            attempt_history=tuple(
                AttemptRecord(
                    attempt=int(item["attempt"]),
                    started_at=_parse_timestamp(item["started_at"]),  # type: ignore[arg-type]
                    finished_at=_parse_timestamp(item["finished_at"]),  # type: ignore[arg-type]
                    outcome=str(item["outcome"]),
                    error_code=item["error_code"],
                )
                for item in history
            ),
        )


class _QueueV3CandidateReader:
    """Minimal claim seam for tests against an unpublished v3 candidate."""

    def __init__(self, path: Path, *, coordinator_path: Path | None = None) -> None:
        self.db_path = Path(path)
        self.state_root = self.db_path.parent.parent
        self.results_dir = self.state_root / "run" / "queue-results"
        self.coordinator_path = coordinator_path

    def ownership_registry(self):
        from operational_ownership import OwnershipRegistry

        if self.coordinator_path is None:
            return OwnershipRegistry(self.state_root)
        return OwnershipRegistry._from_adopted_database(
            self.state_root, self.coordinator_path
        )

    def _connect(self) -> sqlite3.Connection:
        return open_operational_db(
            self.db_path,
            busy_ms=DEFAULTS.queue_busy_ms,
            contract=_QUEUE_V3_CONTRACT,
        )

    @staticmethod
    def _demote_payload_mismatch(
        database: sqlite3.Connection, row: sqlite3.Row, *, now: datetime
    ) -> None:
        changed = database.execute(
            """UPDATE tasks SET state='dead', error_code='payload_hash_mismatch',
                   updated_at=?, lease_owner=NULL, lease_token=NULL,
                   lease_expires_at=NULL, lease_heartbeat_at=NULL,
                   attempt_started_at=NULL, blocked_capability=NULL
               WHERE id=? AND state=?""",
            (_timestamp(now), row["id"], row["state"]),
        ).rowcount
        if changed != 1:
            raise QueueOperationError("payload_demotion_failed")

    @staticmethod
    def _require_valid_task_payload(
        database: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
        parse: bool,
    ) -> PayloadValidation | None:
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=parse
        )
        if validation.code is not None:
            _QueueV3CandidateReader._demote_payload_mismatch(database, row, now=now)
            return None
        return validation

    @staticmethod
    def _require_lease_row(
        database: sqlite3.Connection, lease: QueueLease, now: datetime
    ) -> sqlite3.Row:
        row = database.execute("SELECT * FROM tasks WHERE id=?", (lease.id,)).fetchone()
        if (
            row is None
            or row["state"] != "leased"
            or row["lease_owner"] != lease.owner
            or row["lease_token"] != lease.token
            or row["lease_expires_at"] <= _timestamp(now)
        ):
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        return row

    @staticmethod
    def _raise_payload_mismatch() -> None:
        raise QueueOperationError("payload_hash_mismatch")

    def _deduplicated_task_id(
        self,
        database: sqlite3.Connection,
        dedupe_key: str | None,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
    ) -> str | None:
        """The id of an identical task already enqueued under this dedupe key."""
        if dedupe_key is None:
            return None
        existing = database.execute(
            """SELECT id, kind, handler_version, payload_blob, input_hash
               FROM tasks WHERE dedupe_key=?""",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            return None
        existing_bytes = bytes(existing["payload_blob"])
        current = validate_payload_blob(
            existing_bytes, existing["input_hash"], parse=True
        )
        if current.code is None and _same_enqueued_task(
            existing, existing_bytes, kind, handler_version, payload_bytes, input_hash
        ):
            return str(existing["id"])
        raise QueueOperationError("dedupe_conflict")

    def _insert_task_row(
        self,
        database: sqlite3.Connection,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,dedupe_key,
                   state,priority,created_at,updated_at,available_at
               ) VALUES (?,?,?,?,?,?,'ready',?,?,?,?)""",
            (
                task_id,
                kind,
                handler_version,
                payload_bytes,
                input_hash,
                dedupe_key,
                priority,
                _timestamp(now),
                _timestamp(now),
                _timestamp(ready_at),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("enqueue_failed")

    def _insert_source_links(
        self,
        database: sqlite3.Connection,
        task_id: str,
        normalized_links: list[tuple[str, str]],
    ) -> None:
        for logical_path, source_digest in sorted(normalized_links):
            linked = database.execute(
                """INSERT INTO task_source_links(
                       task_id,logical_path,source_digest
                   ) VALUES (?,?,?)""",
                (task_id, logical_path, source_digest),
            ).rowcount
            if linked != 1:
                raise QueueOperationError("source_link_insert_failed")

    def enqueue(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
        source_links: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    ) -> str:
        _require_enqueue_arguments(kind, handler_version, priority, dedupe_key)
        normalized_links = _normalized_source_links(source_links)
        payload_bytes, input_hash = _validated_payload_bytes(payload)
        now = _utc_now()
        ready_at = _as_utc(available_at or now)
        task_id = uuid.uuid4().hex
        with closing(self._connect()) as database, begin_immediate(database):
            deduplicated = self._deduplicated_task_id(
                database, dedupe_key, kind, handler_version, payload_bytes, input_hash
            )
            if deduplicated is not None:
                return deduplicated
            self._insert_task_row(
                database,
                task_id,
                kind,
                handler_version,
                payload_bytes,
                input_hash,
                dedupe_key,
                priority,
                now,
                ready_at,
            )
            self._insert_source_links(database, task_id, normalized_links)
        return task_id

    def publish_capture_intent(
        self,
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> None:
        _check_capture_intent_descriptor(
            intent_id, intent_path, intent_sha256, byte_size
        )
        now = _timestamp(_utc_now())
        with closing(self._connect()) as database, begin_immediate(database):
            existing = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                _check_ready_intent_identity(
                    existing, intent_path, intent_sha256, byte_size
                )
                return
            inserted = database.execute(
                """INSERT INTO capture_intents(
                       intent_id,relative_path,intent_sha256,byte_size,
                       publication_state,updated_at
                   ) VALUES (?,?,?,?,'ready',?)""",
                (intent_id, intent_path, intent_sha256, byte_size, now),
            ).rowcount
            if inserted != 1:
                raise QueueOperationError("capture_intent_insert_failed")

    def index_capture_intent_pending(
        self,
        *,
        intent_id: str,
        pending_path: str,
        ready_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> str:
        _require_capture_intent_descriptor(
            intent_id, pending_path, intent_sha256, byte_size
        )
        _require_capture_intent_path(ready_path, "ready")
        now = _timestamp(_utc_now())
        with closing(self._connect()) as database, begin_immediate(database):
            existing = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                return _existing_capture_publication_state(
                    existing,
                    pending_path=pending_path,
                    ready_path=ready_path,
                    intent_sha256=intent_sha256,
                    byte_size=byte_size,
                )
            inserted = database.execute(
                """INSERT INTO capture_intents(
                       intent_id,relative_path,intent_sha256,byte_size,
                       publication_state,updated_at
                   ) VALUES (?,?,?,?,'pending',?)""",
                (intent_id, pending_path, intent_sha256, byte_size, now),
            ).rowcount
            if inserted != 1:
                raise QueueOperationError("capture_intent_insert_failed")
        return "pending"

    def mark_capture_intent_ready(
        self,
        *,
        intent_id: str,
        pending_path: str,
        ready_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> None:
        _require_capture_intent_descriptor(
            intent_id, pending_path, intent_sha256, byte_size
        )
        _require_capture_intent_path(ready_path, "ready")
        with closing(self._connect()) as database, begin_immediate(database):
            row = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            state = _existing_capture_publication_state(
                row,
                pending_path=pending_path,
                ready_path=ready_path,
                intent_sha256=intent_sha256,
                byte_size=byte_size,
            )
            if state == "ready":
                return
            updated = database.execute(
                """UPDATE capture_intents
                   SET relative_path=?,publication_state='ready',updated_at=?
                   WHERE intent_id=? AND relative_path=?
                     AND publication_state='pending'""",
                (ready_path, _timestamp(_utc_now()), intent_id, pending_path),
            ).rowcount
            if updated != 1:
                raise QueueOperationError("capture_intent_conflict")

    def enqueue_capture_task_replay_safe(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        capture_fence: object,
        owner: OwnerLease,
    ) -> CaptureTaskBinding:
        payload_bytes = canonical_json_bytes(_redact_payload(dict(payload)))
        dedupe_key = f"capture:{intent_id}:{handler_version}"
        existing = self._capture_replay_binding(
            intent_id=intent_id,
            kind=kind,
            handler_version=handler_version,
            payload_bytes=payload_bytes,
            dedupe_key=dedupe_key,
        )
        if existing is not None:
            return existing
        return self.enqueue_capture_task(
            kind,
            handler_version,
            payload,
            intent_id=intent_id,
            intent_path=intent_path,
            intent_sha256=intent_sha256,
            capture_fence=capture_fence,
            owner=owner,
            dedupe_key=dedupe_key,
        )

    def _capture_replay_binding(
        self,
        *,
        intent_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        dedupe_key: str,
    ) -> CaptureTaskBinding | None:
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT task.*,link.intent_sha256
                   FROM capture_task_links AS link
                   JOIN tasks AS task ON task.id=link.task_id
                   WHERE link.intent_id=?""",
                (intent_id,),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise QueueOperationError("capture_link_conflicted")
            _require_capture_replay_task(
                rows[0], kind, handler_version, payload_bytes, dedupe_key
            )
            return self.active_capture_binding(database, str(rows[0]["id"]))

    @staticmethod
    def _insert_capture_link(
        database: sqlite3.Connection,
        *,
        task_id: str,
        intent_id: str,
        intent_sha256: str,
        handler_version: int,
        link_digest: str,
        created_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO capture_task_links(
                   task_id,intent_id,intent_sha256,handler_version,
                   link_digest,created_at
               ) VALUES (?,?,?,?,?,?)""",
            (
                task_id,
                intent_id,
                intent_sha256,
                handler_version,
                link_digest,
                created_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("capture_link_insert_failed")

    def _require_live_capture_fence(
        self, intent_id: str, capture_fence: object, owner: OwnerLease
    ) -> None:
        """The capture fence must still be the one this owner holds."""
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            try:
                registry.require(coordinator_database, owner)
            except Exception as exc:
                raise QueueOperationError("intent_fence_lost") from exc
            fence_row = coordinator_database.execute(
                """SELECT 1 FROM intent_fences
                   WHERE intent_id=? AND mode='capture' AND token=?
                     AND fencing_epoch=? AND canonical_role=?
                     AND canonical_scope=? AND canonical_actor_id=?
                     AND canonical_owner_token=? AND canonical_fencing_epoch=?
                     AND process_id=? AND process_start_identity=? AND expires_at>?""",
                (
                    intent_id,
                    capture_fence.token,
                    capture_fence.epoch,
                    owner.role,
                    owner.scope,
                    owner.actor_id,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    _timestamp(_utc_now()),
                ),
            ).fetchone()
        if fence_row is None:
            raise QueueOperationError("intent_fence_lost")

    def _insert_capture_task_row(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
        intent_path: str,
        intent_id: str,
        intent_sha256: str,
        link_digest: str,
    ) -> None:
        intent = database.execute(
            "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if not _matching_capture_intent(intent, intent_path, intent_sha256):
            raise QueueOperationError("capture_intent_conflict")
        if dedupe_key is not None and database.execute(
            "SELECT 1 FROM tasks WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone() is not None:
            raise QueueOperationError("dedupe_conflict")
        inserted = database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,dedupe_key,
                   state,priority,created_at,updated_at,available_at
               ) VALUES (?,?,?,?,?,?,'ready',?,?,?,?)""",
            (
                task_id,
                kind,
                handler_version,
                payload_bytes,
                input_hash,
                dedupe_key,
                priority,
                _timestamp(now),
                _timestamp(now),
                _timestamp(ready_at),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("enqueue_failed")
        self._insert_capture_link(
            database,
            task_id=task_id,
            intent_id=intent_id,
            intent_sha256=intent_sha256,
            handler_version=handler_version,
            link_digest=link_digest,
            created_at=_timestamp(now),
        )

    def enqueue_capture_task(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        capture_fence: object,
        owner: OwnerLease,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> CaptureTaskBinding:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        _require_capture_identity(intent_id, intent_sha256, intent_path)
        if not isinstance(owner, OwnerLease) or owner.role != "capture":
            raise ValueError("capture enqueue requires a capture owner")
        _require_capture_fence(capture_fence, intent_id, owner, IntentFence)
        self._require_live_capture_fence(intent_id, capture_fence, owner)
        _require_enqueue_arguments(kind, handler_version, priority, None)
        payload_bytes, input_hash = _validated_capture_payload(payload)
        now = _utc_now()
        ready_at = _as_utc(available_at or now)
        task_id = uuid.uuid4().hex
        link_digest = sha256_bytes(
            canonical_json_bytes(
                _capture_link_record(
                    task_id, intent_id, intent_sha256, handler_version
                )
            )
        )
        with closing(self._connect()) as database, begin_immediate(database):
            self._insert_capture_task_row(
                database,
                task_id=task_id,
                kind=kind,
                handler_version=handler_version,
                payload_bytes=payload_bytes,
                input_hash=input_hash,
                dedupe_key=dedupe_key,
                priority=priority,
                now=now,
                ready_at=ready_at,
                intent_path=intent_path,
                intent_id=intent_id,
                intent_sha256=intent_sha256,
                link_digest=link_digest,
            )
        return CaptureTaskBinding(
            task_id=task_id,
            intent_id=intent_id,
            intent_sha256=intent_sha256,
            handler_version=handler_version,
            active_digest=link_digest,
            seal_digest=None,
        )

    @staticmethod
    def _validate_task_fence_owner(
        owner: OwnerLease, mode: Literal["worker", "queue-operator"]
    ) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(owner, OwnerLease):
            raise TypeError("owner must be an OwnerLease")
        allowed = {
            "worker": {"queue-worker", "compile", "doctor", "nightly", "weekly"},
            "queue-operator": {"queue-operator", "repair"},
        }
        if mode not in allowed or owner.role not in allowed[mode]:
            raise ValueError("owner cannot acquire the requested task fence")

    def acquire_task_fence(
        self,
        task_id: str,
        *,
        mode: Literal["worker", "queue-operator"],
        owner: OwnerLease,
    ) -> TaskFence:
        self._validate_task_fence_owner(owner, mode)
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be non-empty")
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        now = _utc_now()
        expires_at = min(owner.expires_at, now + timedelta(seconds=120))
        token = uuid.uuid4().hex
        with closing(self._connect()) as database, begin_immediate(database):
            projection = database.execute(
                """SELECT 1 FROM queue_ownership
                   WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                     AND owner_token=? AND fencing_epoch=? AND process_id=?
                     AND process_start_identity=? AND expires_at>?""",
                (
                    owner.actor_id,
                    owner.role,
                    owner.scope,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    _timestamp(now),
                ),
            ).fetchone()
            if projection is None:
                raise QueueOperationError("queue_owner_fence_lost")
            if database.execute(
                "SELECT 1 FROM tasks WHERE id=?", (task_id,)
            ).fetchone() is None:
                raise KeyError(task_id)
            existing = database.execute(
                "SELECT * FROM task_fences WHERE task_id=?", (task_id,)
            ).fetchone()
            if existing is not None:
                raise QueueOperationError("task_fenced")
            epoch = int(
                database.execute(
                    """INSERT INTO task_fence_epochs(task_id,last_epoch) VALUES (?,1)
                       ON CONFLICT(task_id) DO UPDATE
                       SET last_epoch=task_fence_epochs.last_epoch+1
                       RETURNING last_epoch""",
                    (task_id,),
                ).fetchone()[0]
            )
            inserted = database.execute(
                """INSERT INTO task_fences(
                       task_id,mode,token,fencing_epoch,canonical_role,
                       canonical_scope,canonical_actor_id,canonical_owner_token,
                       canonical_fencing_epoch,process_id,process_start_identity,
                       heartbeat_at,expires_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    mode,
                    token,
                    epoch,
                    owner.role,
                    owner.scope,
                    owner.actor_id,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    _timestamp(now),
                    _timestamp(expires_at),
                ),
            ).rowcount
            if inserted != 1:
                raise QueueOperationError("task_fence_failed")
        return TaskFence(task_id, mode, token, epoch, owner, expires_at)

    def release_task_fence(self, fence: TaskFence) -> None:
        if not isinstance(fence, TaskFence):
            raise TypeError("fence must be a TaskFence")
        with closing(self._connect()) as database, begin_immediate(database):
            deleted = database.execute(
                """DELETE FROM task_fences
                   WHERE task_id=? AND mode=? AND token=? AND fencing_epoch=?
                     AND canonical_role=? AND canonical_scope=?
                     AND canonical_actor_id=? AND canonical_owner_token=?
                     AND canonical_fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    fence.task_id,
                    fence.mode,
                    fence.token,
                    fence.epoch,
                    fence.owner.role,
                    fence.owner.scope,
                    fence.owner.actor_id,
                    fence.owner.token,
                    fence.owner.epoch,
                    fence.owner.process.pid,
                    fence.owner.process.start_identity,
                ),
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("task_fence_lost")

    @contextmanager
    def task_fence(
        self,
        task_id: str,
        *,
        mode: Literal["worker", "queue-operator"],
        owner: OwnerLease,
    ) -> Iterator[TaskFence]:
        fence = self.acquire_task_fence(task_id, mode=mode, owner=owner)
        try:
            yield fence
        finally:
            self.release_task_fence(fence)

    @contextmanager
    def _corrupt_intent_fence(
        self, task_id: str, *, owner: OwnerLease
    ) -> Iterator[tuple[CaptureTaskBinding | None, object | None]]:
        try:
            binding = self.active_capture_binding(None, task_id)
        except QueueOperationError:
            yield None, None
            return
        if binding.intent_id is None:
            yield binding, None
            return
        from markdown_transaction import MarkdownCoordinator

        coordinator = MarkdownCoordinator._from_v3_candidate(
            self.state_root / "run" / "markdown-transactions-v3.candidate.sqlite3",
            state_root=self.state_root,
        )
        fence = coordinator.acquire_intent_fence(
            binding.intent_id, mode="operator", owner=owner
        )
        try:
            yield binding, fence
        finally:
            coordinator.release_intent_fence(fence)

    def active_capture_binding(
        self, connection: sqlite3.Connection | None, task_id: str
    ) -> CaptureTaskBinding:
        owned = connection is None
        database = self._connect() if owned else connection
        try:
            return _capture_binding_of(database, task_id)
        finally:
            if owned:
                database.close()

    def append_capture_link_resolution(
        self,
        task_id: str,
        *,
        supersedes_digest: str | None,
        observed: Mapping[str, object],
        selected_intent: Mapping[str, object] | None,
        owner: OwnerLease,
        reason: str,
    ) -> CaptureTaskBinding:
        from operational_ownership import OwnerLease, current_actor_identity

        if not isinstance(owner, OwnerLease) or owner.role != "repair":
            raise ValueError("capture link resolution requires a repair owner")
        _check_resolution_reason(reason)
        actor = current_actor_identity()
        now = _utc_now()
        record = _capture_resolution_record(
            task_id=task_id,
            supersedes_digest=supersedes_digest,
            observed=observed,
            selected_intent=selected_intent,
            actor=actor,
            reason=reason,
            now=now,
        )
        resolution_digest = sha256_bytes(canonical_json_bytes(record))
        observed_json = canonical_json_bytes(record["observed"])
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        with closing(self._connect()) as database, begin_immediate(database):
            _require_repair_projection(database, owner, now)
            _require_unfenced_unsealed_task(database, task_id)
            active = self.active_capture_binding(database, task_id)
            if supersedes_digest != active.active_digest:
                raise QueueOperationError("capture_link_conflicted")
            selected_id = _selected_intent_id(database, selected_intent)
            _insert_link_resolution(
                database,
                resolution_digest=resolution_digest,
                task_id=task_id,
                supersedes_digest=supersedes_digest,
                observed_json=observed_json,
                selected_id=selected_id,
                actor=actor,
                reason=reason,
                now=now,
            )
            return self.active_capture_binding(database, task_id)

    def seal_capture_binding(
        self,
        task_id: str,
        *,
        consumer_kind: Literal["transaction", "terminal", "corrupt-disposition"],
        consumer_id: str,
        active_link_digest: str,
        before_side_effect: Callable[[], None] | None = None,
    ) -> CaptureTaskBinding:
        _check_consumer_kind(consumer_kind)
        _check_consumer_id(consumer_id)
        now = _utc_now()
        seal_digest = _capture_seal_digest(
            task_id=task_id,
            consumer_kind=consumer_kind,
            consumer_id=consumer_id,
            active_link_digest=active_link_digest,
        )
        with closing(self._connect()) as database, begin_immediate(database):
            active = self.active_capture_binding(database, task_id)
            if active.active_digest != active_link_digest:
                raise QueueOperationError("capture_link_conflicted")
            _record_capture_seal(
                database,
                task_id=task_id,
                consumer_kind=consumer_kind,
                consumer_id=consumer_id,
                active_link_digest=active_link_digest,
                seal_digest=seal_digest,
                now=now,
            )
        if before_side_effect is not None:
            before_side_effect()
        return CaptureTaskBinding(
            task_id=active.task_id,
            intent_id=active.intent_id,
            intent_sha256=active.intent_sha256,
            handler_version=active.handler_version,
            active_digest=active.active_digest,
            seal_digest=seal_digest,
        )

    @staticmethod
    def _insert_semantic_decision(
        database: sqlite3.Connection,
        *,
        intent_id: str,
        stage: str,
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        published_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO semantic_decisions(
                   intent_id,stage,decision_path,decision_sha256,
                   active_link_digest,publication_state,published_at
               ) VALUES (?,?,?,?,?,'published',?)""",
            (
                intent_id,
                stage,
                decision_path,
                decision_sha256,
                active_link_digest,
                published_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("semantic_decision_insert_failed")

    def indexed_capture_decision(
        self,
        *,
        task_id: str,
        intent_id: str,
        stage: Literal["flush", "feedback", "feedback-verify"],
        active_link_digest: str,
    ) -> SemanticDecision | None:
        _require_capture_decision_path(
            _capture_decision_relative_path(intent_id, stage), intent_id, stage
        )
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT * FROM semantic_decisions WHERE intent_id=? AND stage=?",
                (intent_id, stage),
            ).fetchone()
            if row is None:
                return None
            active = self.active_capture_binding(database, task_id)
            _require_indexed_capture_decision(
                row,
                active,
                intent_id=intent_id,
                stage=stage,
                active_link_digest=active_link_digest,
            )
            published_at = _indexed_capture_decision_published_at(row)
        decision_path, decision_sha256 = _read_indexed_capture_decision(
            self.state_root, row
        )
        return SemanticDecision(
            task_id=task_id,
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=active_link_digest,
            seal_digest=active.seal_digest or "",
            published_at=published_at,
        )

    def _read_semantic_decision_bytes(
        self, decision_path: str, decision_sha256: str, intent_id: str, stage: str
    ) -> None:
        _require_capture_decision_path(decision_path, intent_id, stage)
        data = read_runtime_bytes(
            self.state_root / decision_path,
            self.state_root,
            max_bytes=64 * 1024,
            owner_only=True,
        )
        if sha256_bytes(data) != decision_sha256:
            raise QueueOperationError("semantic_decision_conflict")

    def _require_live_worker_intent(
        self, intent_id: str, intent_fence: object, owner: OwnerLease, now: datetime
    ) -> None:
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
            intent_row = coordinator_database.execute(
                """SELECT 1 FROM intent_fences WHERE intent_id=? AND mode='worker'
                   AND token=? AND fencing_epoch=? AND canonical_owner_token=?
                   AND canonical_fencing_epoch=? AND expires_at>?""",
                (
                    intent_id,
                    intent_fence.token,
                    intent_fence.epoch,
                    owner.token,
                    owner.epoch,
                    _timestamp(now),
                ),
            ).fetchone()
        if intent_row is None:
            raise QueueOperationError("intent_fence_lost")

    def _require_live_task_fence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task_fence: TaskFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        projection = database.execute(
            """SELECT 1 FROM queue_ownership WHERE owner_token=?
               AND fencing_epoch=? AND expires_at>?""",
            (owner.token, owner.epoch, _timestamp(now)),
        ).fetchone()
        fence_row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_id,
                task_fence.token,
                task_fence.epoch,
                owner.token,
                owner.epoch,
                _timestamp(now),
            ),
        ).fetchone()
        if projection is None or fence_row is None:
            raise QueueOperationError("task_fence_lost")

    def _require_unsealed_capture_link(
        self,
        database: sqlite3.Connection,
        task_id: str,
        intent_id: str,
        active_link_digest: str,
    ) -> None:
        active = self.active_capture_binding(database, task_id)
        if active.intent_id != intent_id:
            raise QueueOperationError("capture_link_conflicted")
        if active.active_digest != active_link_digest:
            raise QueueOperationError("capture_link_conflicted")
        if active.seal_digest is not None:
            raise QueueOperationError("capture_link_conflicted")

    def _seal_semantic_decision(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        intent_id: str,
        stage: str,
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        seal_digest: str,
        now: datetime,
    ) -> None:
        existing = database.execute(
            """SELECT seal.*, decision.decision_path,
                      decision.decision_sha256,decision.active_link_digest
               FROM capture_task_link_seals AS seal
               LEFT JOIN semantic_decisions AS decision
                 ON decision.intent_id=? AND decision.stage=?
               WHERE seal.task_id=?""",
            (intent_id, stage, task_id),
        ).fetchone()
        if existing is not None:
            if not _semantic_decision_matches(
                existing,
                intent_id,
                stage,
                seal_digest,
                decision_path,
                decision_sha256,
                active_link_digest,
            ):
                raise QueueOperationError("semantic_decision_conflict")
            return
        inserted = database.execute(
            """INSERT INTO capture_task_link_seals(
                   task_id,active_digest,consumer_kind,consumer_id,
                   seal_digest,sealed_at
               ) VALUES (?,?,'semantic-decision',?,?,?)""",
            (
                task_id,
                active_link_digest,
                f"{intent_id}:{stage}",
                seal_digest,
                _timestamp(now),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("capture_link_seal_failed")
        self._insert_semantic_decision(
            database,
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=active_link_digest,
            published_at=_timestamp(now),
        )

    def _published_decision_time(
        self, database: sqlite3.Connection, intent_id: str, stage: str
    ) -> datetime:
        published = database.execute(
            """SELECT published_at FROM semantic_decisions
               WHERE intent_id=? AND stage=?""",
            (intent_id, stage),
        ).fetchone()
        if published is None:
            raise QueueOperationError("semantic_decision_conflict")
        published_at = _parse_timestamp(str(published["published_at"]))
        if published_at is None:
            raise QueueOperationError("semantic_decision_conflict")
        return published_at

    def publish_semantic_decision(
        self,
        coordinator: object,
        *,
        task_id: str,
        intent_id: str,
        stage: Literal["flush", "feedback", "feedback-verify"],
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> SemanticDecision:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        _require_semantic_decision_fences(
            task_id,
            intent_id,
            stage,
            decision_sha256,
            task_fence,
            intent_fence,
            owner,
            IntentFence,
            OwnerLease,
        )
        self._read_semantic_decision_bytes(
            decision_path, decision_sha256, intent_id, stage
        )
        now = _utc_now()
        self._require_live_worker_intent(intent_id, intent_fence, owner, now)
        seal_digest = _semantic_seal_digest(
            task_id, intent_id, stage, active_link_digest
        )
        with closing(self._connect()) as database, begin_immediate(database):
            self._require_live_task_fence(database, task_id, task_fence, owner, now)
            self._require_unsealed_capture_link(
                database, task_id, intent_id, active_link_digest
            )
            self._seal_semantic_decision(
                database,
                task_id=task_id,
                intent_id=intent_id,
                stage=stage,
                decision_path=decision_path,
                decision_sha256=decision_sha256,
                active_link_digest=active_link_digest,
                seal_digest=seal_digest,
                now=now,
            )
            published_at = self._published_decision_time(database, intent_id, stage)
        return SemanticDecision(
            task_id=task_id,
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=active_link_digest,
            seal_digest=seal_digest,
            published_at=published_at,
        )

    def _read_capture_terminal(
        self, terminal_path: str, terminal_sha256: str
    ) -> tuple[dict[str, object], bytes]:
        data = read_runtime_bytes(
            self.state_root / terminal_path,
            self.state_root,
            max_bytes=64 * 1024,
            owner_only=True,
        )
        if sha256_bytes(data) != terminal_sha256:
            raise QueueOperationError("capture_terminal_conflict")
        value = json.loads(data.decode("utf-8", errors="strict"))
        if not isinstance(value, dict):
            raise QueueOperationError("capture_terminal_invalid")
        return value, data

    def _capture_terminal_decisions(
        self, database: sqlite3.Connection, intent_id: str
    ) -> list[dict[str, str]]:
        rows = database.execute(
            """SELECT stage,decision_path,decision_sha256 FROM semantic_decisions
               WHERE intent_id=? ORDER BY stage""",
            (intent_id,),
        ).fetchall()
        result = []
        for row in rows:
            data = read_runtime_bytes(
                self.state_root / str(row["decision_path"]),
                self.state_root,
                max_bytes=1024 * 1024,
                owner_only=True,
            )
            if sha256_bytes(data) != row["decision_sha256"]:
                raise QueueOperationError("semantic_decision_conflict")
            result.append(
                {
                    "stage": str(row["stage"]),
                    "decision_path": str(row["decision_path"]),
                    "decision_sha256": str(row["decision_sha256"]),
                }
            )
        return result

    @staticmethod
    def _require_capture_terminal_disposition(
        record: Mapping[str, object], decisions: list[dict[str, str]]
    ) -> None:
        disposition = record.get("disposition")
        if not isinstance(disposition, dict):
            raise QueueOperationError("capture_terminal_invalid")
        required = _capture_terminal_disposition_fields(disposition.get("kind"))
        decision_digests = {item["decision_sha256"] for item in decisions}
        actual = (set(disposition), disposition.get("decision_sha256") in decision_digests)
        expected = (required, True)
        if actual != expected:
            raise QueueOperationError("capture_terminal_invalid")

    def _require_capture_terminal_record(
        self,
        record: Mapping[str, object],
        *,
        binding: CaptureTaskBinding,
        decisions: list[dict[str, str]],
    ) -> None:
        expected_keys = {
            "schema_version",
            "intent_id",
            "intent_sha256",
            "semantic_decisions",
            "processing_binding",
            "disposition",
        }
        processing = record.get("processing_binding")
        identity = (
            set(record),
            record.get("schema_version"),
            record.get("intent_id"),
            record.get("intent_sha256"),
            record.get("semantic_decisions"),
        )
        expected_identity = (
            expected_keys,
            "capture-terminal/v1",
            binding.intent_id,
            binding.intent_sha256,
            decisions,
        )
        if identity != expected_identity or not isinstance(processing, dict):
            raise QueueOperationError("capture_terminal_invalid")
        expected_processing = {
            "kind": "task",
            "task_id": binding.task_id,
            "active_link_digest": binding.active_digest,
        }
        if processing != expected_processing:
            raise QueueOperationError("capture_terminal_invalid")
        self._require_capture_terminal_disposition(record, decisions)

    def _require_capture_terminal_owner(
        self,
        *,
        lease: QueueLease,
        intent_id: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> None:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        actual = (
            isinstance(owner, OwnerLease),
            task_fence.task_id,
            task_fence.mode,
            task_fence.owner,
            isinstance(intent_fence, IntentFence),
            getattr(intent_fence, "intent_id", None),
            getattr(intent_fence, "mode", None),
            getattr(intent_fence, "owner", None),
        )
        expected = (True, lease.id, "worker", owner, True, intent_id, "worker", owner)
        if actual != expected:
            raise ValueError("capture terminal fences do not match")

    @staticmethod
    def _require_capture_terminal_task_fence(
        database: sqlite3.Connection,
        task_fence: TaskFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND mode='worker'
               AND token=? AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_fence.task_id,
                task_fence.token,
                task_fence.epoch,
                owner.token,
                owner.epoch,
                _timestamp(now),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("task_fence_lost")

    @staticmethod
    def _require_capture_terminal_intent_fence(
        database: sqlite3.Connection,
        intent_fence: IntentFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM intent_fences WHERE intent_id=? AND mode='worker'
               AND token=? AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                intent_fence.intent_id,
                intent_fence.token,
                intent_fence.epoch,
                owner.token,
                owner.epoch,
                _timestamp(now),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("intent_fence_lost")

    @staticmethod
    def _commit_capture_terminal_row(
        database: sqlite3.Connection,
        row: sqlite3.Row,
        lease: QueueLease,
        *,
        terminal_path: str,
        terminal_sha256: str,
        intent_id: str,
        now: datetime,
    ) -> None:
        database.execute(
            """INSERT INTO attempt_history(
                   task_id,attempt,started_at,finished_at,outcome,error_code
               ) VALUES (?,?,?,?, 'succeeded',NULL)""",
            (
                lease.id,
                row["attempts"],
                row["attempt_started_at"] or _timestamp(now),
                _timestamp(now),
            ),
        )
        changed = database.execute(
            """UPDATE tasks SET state='succeeded',result_reference=?,result_sha256=?,
                   result_operation_id=?,error_code=NULL,updated_at=?,last_attempt_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   lease_heartbeat_at=NULL,attempt_started_at=NULL
               WHERE id=? AND lease_token=? AND state='leased'""",
            (
                terminal_path,
                terminal_sha256,
                f"capture-terminal:{intent_id}",
                _timestamp(now),
                _timestamp(now),
                lease.id,
                lease.token,
            ),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def complete_existing_capture_terminal(
        self,
        lease: QueueLease,
        *,
        intent_id: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> str | None:
        relative = f"run/queue-results/capture-{intent_id}.json"
        candidate = self.state_root / relative
        try:
            candidate.lstat()
        except FileNotFoundError:
            return None
        data = read_runtime_bytes(
            candidate,
            self.state_root,
            max_bytes=64 * 1024,
            owner_only=True,
        )
        return self.complete_capture_terminal(
            lease,
            intent_id=intent_id,
            terminal_path=relative,
            terminal_sha256=sha256_bytes(data),
            active_link_digest=active_link_digest,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )

    def complete_capture_terminal(
        self,
        lease: QueueLease,
        *,
        intent_id: str,
        terminal_path: str,
        terminal_sha256: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> str:
        _require_lower_sha256(intent_id, "intent_id")
        _require_lower_sha256(terminal_sha256, "terminal_sha256")
        _require_capture_terminal_path(terminal_path, intent_id)
        self._require_capture_terminal_owner(
            lease=lease,
            intent_id=intent_id,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )
        record, _data = self._read_capture_terminal(terminal_path, terminal_sha256)
        registry = self.ownership_registry()
        now = _utc_now()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
            self._require_capture_terminal_intent_fence(
                coordinator_database, intent_fence, owner, now
            )
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            self._require_capture_terminal_task_fence(database, task_fence, owner, now)
            binding = self.active_capture_binding(database, lease.id)
            if binding.active_digest != active_link_digest or binding.intent_id != intent_id:
                raise QueueOperationError("capture_link_conflicted")
            decisions = self._capture_terminal_decisions(database, intent_id)
            self._require_capture_terminal_record(record, binding=binding, decisions=decisions)
            self._commit_capture_terminal_row(
                database,
                row,
                lease,
                terminal_path=terminal_path,
                terminal_sha256=terminal_sha256,
                intent_id=intent_id,
                now=now,
            )
        return terminal_path

    @staticmethod
    def _publish_corrupt_fixed_files(
        package: Path,
        *,
        payload: bytes,
        history: bytes,
        metadata: bytes,
    ) -> None:
        package.parent.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(package.parent, 0o700)
        if not package.exists():
            package.mkdir()
            _harden_owner_only(package, 0o700)
        for name, data in (
            ("payload.bin", payload),
            ("attempt-history.json", history),
            ("task-metadata.json", metadata),
        ):
            _write_durable_file(package / name, data)

    @staticmethod
    def _insert_corrupt_disposition(
        database: sqlite3.Connection,
        *,
        task_id: str,
        operation_id: str,
        package_path: str,
        manifest_sha256: str,
        disposition_sha256: str,
        active_link_digest: str | None,
        disposed_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO corrupt_dispositions(
                   task_id,operation_id,package_path,manifest_sha256,
                   disposition_sha256,active_link_digest,disposed_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                task_id,
                operation_id,
                package_path,
                manifest_sha256,
                disposition_sha256,
                active_link_digest,
                disposed_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_disposition_failed")

    @staticmethod
    def _corrupt_task_row(
        database: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row:
        """The task being quarantined, in a state that allows it."""
        row = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row["state"] not in {"dead", "quarantine_pending"}:
            raise QueueOperationError("corrupt_quarantine_state_invalid")
        return row

    @staticmethod
    def _corrupt_attempt_history(
        database: sqlite3.Connection, task_id: str
    ) -> bytes:
        rows = database.execute(
            """SELECT attempt,started_at,finished_at,outcome,error_code
               FROM attempt_history WHERE task_id=? ORDER BY sequence""",
            (task_id,),
        ).fetchall()
        return canonical_json_bytes([_attempt_history_entry(item) for item in rows])

    def _corrupt_evidence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        reason: str,
        actor: str,
    ) -> _CorruptEvidence:
        """Everything the export is derived from, read under one transaction."""
        row = self._corrupt_task_row(database, task_id)
        raw = bytes(row["payload_blob"])
        raw_sha256 = validate_payload_blob(
            raw, row["input_hash"], parse=False
        ).input_hash
        history = self._corrupt_attempt_history(database, task_id)
        metadata = _corrupt_metadata(task_id, row)
        history_sha256 = sha256_bytes(history)
        metadata_sha256 = sha256_bytes(metadata)
        return _CorruptEvidence(
            raw=raw,
            history=history,
            metadata=metadata,
            raw_sha256=raw_sha256,
            history_sha256=history_sha256,
            metadata_sha256=metadata_sha256,
            disposition_key=_corrupt_disposition_key(
                actor=actor,
                history_sha256=history_sha256,
                metadata_sha256=metadata_sha256,
                raw_sha256=raw_sha256,
                reason=reason,
                task_id=task_id,
            ),
            lineage_generation=int(row["lineage_generation"]),
        )

    def _begin_corrupt_export(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        evidence: _CorruptEvidence,
        operation_id: str,
        task_fence: object,
        actor: str,
        reason: str,
        now: datetime,
    ) -> None:
        """Record a new export operation and move the task into quarantine."""
        inserted = database.execute(
            """INSERT INTO corrupt_export_operations(
                   operation_id,task_id,disposition_key,
                   task_fence_token_digest,task_fence_epoch,
                   intent_fence_digest,raw_sha256,history_sha256,
                   metadata_sha256,lineage_generation,cursor_task_id,
                   link_count,page_count,rolling_root,state,
                   actor_identity,reason,created_at,updated_at
               ) VALUES (?,?,?,?,?,NULL,?,?,?,?, '',0,0,?,'exporting',?,?,?,?)""",
            (
                operation_id,
                task_id,
                evidence.disposition_key,
                sha256_bytes(task_fence.token.encode("utf-8")),
                task_fence.epoch,
                evidence.raw_sha256,
                evidence.history_sha256,
                evidence.metadata_sha256,
                evidence.lineage_generation,
                sha256_bytes(b""),
                actor,
                reason,
                _timestamp(now),
                _timestamp(now),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_export_start_failed")
        changed = database.execute(
            """UPDATE tasks SET state='quarantine_pending',updated_at=?
               WHERE id=? AND state='dead'""",
            (_timestamp(now), task_id),
        ).rowcount
        if changed != 1:
            raise QueueOperationError("corrupt_quarantine_start_failed")

    @staticmethod
    def _corrupt_export_conflict(
        database: sqlite3.Connection, existing: sqlite3.Row, task_id: str
    ) -> CorruptExportProgress:
        """A different export already ran; a disposed one is reported as done."""
        if existing["state"] == "disposed":
            disposition = database.execute(
                "SELECT * FROM corrupt_dispositions WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if disposition is not None:
                return CorruptExportProgress(
                    task_id=task_id,
                    operation_id=str(existing["operation_id"]),
                    state="quarantined",
                    pages_written=int(existing["page_count"]),
                    links_exported=int(existing["link_count"]),
                    complete=True,
                    code=None,
                )
        raise QueueOperationError("corrupt_export_conflict")

    def _open_corrupt_export(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        evidence: _CorruptEvidence,
        task_fence: object,
        actor: str,
        reason: str,
        now: datetime,
    ) -> tuple[str, str] | CorruptExportProgress:
        """The operation to continue, or the progress of one already finished."""
        operation_id = f"corrupt-export:{evidence.disposition_key}"
        existing = database.execute(
            "SELECT * FROM corrupt_export_operations WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if existing is None:
            self._begin_corrupt_export(
                database,
                task_id=task_id,
                evidence=evidence,
                operation_id=operation_id,
                task_fence=task_fence,
                actor=actor,
                reason=reason,
                now=now,
            )
            return operation_id, evidence.disposition_key
        if (
            existing["operation_id"] == operation_id
            and existing["disposition_key"] == evidence.disposition_key
        ):
            return str(existing["operation_id"]), str(existing["disposition_key"])
        return self._corrupt_export_conflict(database, existing, task_id)

    def _advance_corrupt_export_once(
        self, task_id: str, operation_id: str, package: Path
    ) -> None:
        """Move an in-progress export one page forward, under its own lock."""
        with closing(self._connect()) as database, begin_immediate(database):
            operation = database.execute(
                "SELECT * FROM corrupt_export_operations WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if operation is None:
                raise QueueOperationError("corrupt_export_lost")
            if operation["state"] == "exporting":
                self._advance_corrupt_export(
                    database, operation, operation_id, task_id, package
                )

    def _corrupt_export_operation(self, task_id: str) -> sqlite3.Row:
        with closing(self._connect()) as database:
            operation = database.execute(
                "SELECT * FROM corrupt_export_operations WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if operation is None:
            raise QueueOperationError("corrupt_export_lost")
        return operation

    def _quarantine_corrupt_owned(
        self,
        task_id: str,
        *,
        reason: str,
        actor: str,
        task_fence: object,
        capture_binding: object,
        intent_fence: object,
    ) -> CorruptExportProgress:
        """Export and dispose one corrupt task while holding every fence."""
        now = _utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            evidence = self._corrupt_evidence(database, task_id, reason, actor)
            opened = self._open_corrupt_export(
                database,
                task_id=task_id,
                evidence=evidence,
                task_fence=task_fence,
                actor=actor,
                reason=reason,
                now=now,
            )
        if isinstance(opened, CorruptExportProgress):
            return opened
        operation_id, disposition_key = opened
        package = self.results_dir / f"corrupt-{disposition_key}"
        self._publish_corrupt_fixed_files(
            package,
            payload=evidence.raw,
            history=evidence.history,
            metadata=evidence.metadata,
        )
        self._advance_corrupt_export_once(task_id, operation_id, package)
        operation = self._corrupt_export_operation(task_id)
        if operation["state"] != "manifested":
            return CorruptExportProgress(
                task_id=task_id,
                operation_id=operation_id,
                state="quarantine_pending",
                pages_written=int(operation["page_count"]),
                links_exported=int(operation["link_count"]),
                complete=False,
                code=None,
            )
        return self._dispose_corrupt_export(
            operation,
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
            package=package,
            raw=evidence.raw,
            history=evidence.history,
            metadata=evidence.metadata,
            actor=actor,
            reason=reason,
            task_fence=task_fence,
            capture_binding=capture_binding,
            intent_fence=intent_fence,
        )

    def quarantine_corrupt(
        self,
        task_id: str,
        *,
        reason: str,
        owner: OwnerLease,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> CorruptExportProgress:
        from operational_ownership import OwnerLease, current_actor_identity

        _require_active(deadline, cancelled)
        if not isinstance(owner, OwnerLease) or owner.role != "repair":
            raise ValueError("corrupt quarantine requires a repair owner")
        _check_quarantine_names(task_id, reason)
        registry = self.ownership_registry()
        actor = current_actor_identity()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        with self.queue_owner(
            role="queue-operator",
            scope=f"task:{sha256_bytes(task_id.encode('utf-8'))}",
            parent=owner,
        ), self.task_fence(
            task_id, mode="queue-operator", owner=owner
        ) as task_fence, self._corrupt_intent_fence(
            task_id, owner=owner
        ) as (capture_binding, intent_fence):
            return self._quarantine_corrupt_owned(
                task_id,
                reason=reason,
                actor=actor,
                task_fence=task_fence,
                capture_binding=capture_binding,
                intent_fence=intent_fence,
            )

    def _dispose_corrupt_export(
        self,
        operation: sqlite3.Row,
        *,
        operation_id: str,
        task_id: str,
        disposition_key: str,
        package: Path,
        raw: bytes,
        history: bytes,
        metadata: bytes,
        actor: str,
        reason: str,
        task_fence: object,
        capture_binding: object,
        intent_fence: object,
    ) -> CorruptExportProgress:
        """Publish the manifest and disposition, then retire the task."""
        manifest = _corrupt_manifest(
            operation,
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
        )
        _write_durable_file(package / "manifest.json", canonical_json_bytes(manifest))
        if capture_binding is None or intent_fence is None:
            return _blocked_corrupt_progress(
                operation, task_id=task_id, operation_id=operation_id
            )
        sealed = self.seal_capture_binding(
            task_id,
            consumer_kind="corrupt-disposition",
            consumer_id=operation_id,
            active_link_digest=capture_binding.active_digest,
        )
        manifest_sha256 = sha256_bytes(
            _read_stable_owner_file(package / "manifest.json", 64 * 1024)
        )
        disposed_at = str(operation["created_at"])
        disposition_record = _corrupt_disposition_record(
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
            manifest_sha256=manifest_sha256,
            active_link_digest=sealed.active_digest,
            actor=actor,
            reason=reason,
            disposed_at=disposed_at,
        )
        disposition_bytes = canonical_json_bytes(disposition_record)
        _write_durable_file(package / "disposition.json", disposition_bytes)
        _verify_corrupt_package_bytes(
            package,
            raw=raw,
            history=history,
            metadata=metadata,
            disposition_bytes=disposition_bytes,
        )
        self._commit_corrupt_disposition(
            task_id=task_id,
            operation_id=operation_id,
            sealed=sealed,
            task_fence=task_fence,
            disposition_record=disposition_record,
            manifest_sha256=manifest_sha256,
            disposition_sha256=sha256_bytes(disposition_bytes),
            disposed_at=disposed_at,
        )
        return _quarantined_corrupt_progress(
            operation, task_id=task_id, operation_id=operation_id
        )

    def _commit_corrupt_disposition(
        self,
        *,
        task_id: str,
        operation_id: str,
        sealed: object,
        task_fence: object,
        disposition_record: Mapping[str, object],
        manifest_sha256: str,
        disposition_sha256: str,
        disposed_at: str,
    ) -> None:
        """Retire the task, once everything it depends on still holds."""
        with closing(self._connect()) as database, begin_immediate(database):
            self._require_disposable_corrupt_state(
                database,
                task_id=task_id,
                operation_id=operation_id,
                sealed=sealed,
                task_fence=task_fence,
            )
            self._insert_corrupt_disposition(
                database,
                task_id=task_id,
                operation_id=operation_id,
                package_path=str(disposition_record["package_path"]),
                manifest_sha256=manifest_sha256,
                disposition_sha256=disposition_sha256,
                active_link_digest=sealed.active_digest,
                disposed_at=disposed_at,
            )
            self._retire_quarantined_task(database, task_id, operation_id)

    @staticmethod
    def _require_disposable_corrupt_state(
        database: sqlite3.Connection,
        *,
        task_id: str,
        operation_id: str,
        sealed: object,
        task_fence: object,
    ) -> None:
        """The task, the operation, the fence and the seal all still agree."""
        current_task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        current_operation = database.execute(
            "SELECT * FROM corrupt_export_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        current_fence = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND expires_at>?""",
            (task_id, task_fence.token, task_fence.epoch, _timestamp(_utc_now())),
        ).fetchone()
        current_seal = database.execute(
            """SELECT 1 FROM capture_task_link_seals
               WHERE task_id=? AND active_digest=? AND seal_digest=?
                 AND consumer_kind='corrupt-disposition' AND consumer_id=?""",
            (task_id, sealed.active_digest, sealed.seal_digest, operation_id),
        ).fetchone()
        incoming_count = database.execute(
            "SELECT COUNT(*) FROM tasks WHERE redrive_of=?", (task_id,)
        ).fetchone()[0]
        if current_task is None or current_operation is None:
            raise QueueOperationError("corrupt_export_verification_failed")
        if current_fence is None or current_seal is None:
            raise QueueOperationError("corrupt_export_verification_failed")
        _require_disposable_rows(current_task, current_operation, incoming_count)

    @staticmethod
    def _retire_quarantined_task(
        database: sqlite3.Connection, task_id: str, operation_id: str
    ) -> None:
        """Both rows move together, or neither does."""
        changed = database.execute(
            """UPDATE tasks SET state='quarantined',updated_at=?
               WHERE id=? AND state='quarantine_pending'""",
            (_timestamp(_utc_now()), task_id),
        ).rowcount
        advanced = database.execute(
            """UPDATE corrupt_export_operations
               SET state='disposed',updated_at=?
               WHERE operation_id=? AND state='manifested'""",
            (_timestamp(_utc_now()), operation_id),
        ).rowcount
        if (changed, advanced) != (1, 1):
            raise QueueOperationError("corrupt_disposition_failed")

    def _bounded_lineage_links(
        self,
        candidates: list,
        operation: sqlite3.Row,
        operation_id: str,
        started: float,
    ) -> list[dict[str, object]]:
        """As many lineage links as fit one bounded page inside its time slice."""
        links: list[dict[str, object]] = []
        for child in candidates[:1000]:
            candidate = {
                "created_at": str(child["created_at"]),
                "input_hash": str(child["input_hash"]),
                "state": str(child["state"]),
                "task_id": str(child["id"]),
                "updated_at": str(child["updated_at"]),
            }
            prospective = canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "page_number": int(operation["page_count"]) + 1,
                    "previous_root": str(operation["rolling_root"]),
                    "links": [*links, candidate],
                }
            )
            if len(prospective) > 1024 * 1024 or time.monotonic() - started >= 5:
                break
            links.append(candidate)
        return links

    def _write_lineage_page(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        package: Path,
        links: list[dict[str, object]],
        candidates: list,
    ) -> None:
        page_number = int(operation["page_count"]) + 1
        page_bytes = canonical_json_bytes(
            {
                "operation_id": operation_id,
                "page_number": page_number,
                "previous_root": str(operation["rolling_root"]),
                "links": links,
            }
        )
        page_sha256 = sha256_bytes(page_bytes)
        rolling_root = sha256_bytes(
            bytes.fromhex(str(operation["rolling_root"]))
            + bytes.fromhex(page_sha256)
        )
        page_path = package / f"lineage-page-{page_number:08d}.json"
        _write_durable_file(page_path, page_bytes)
        if _read_stable_owner_file(page_path, 1024 * 1024) != page_bytes:
            raise QueueOperationError("corrupt_page_verification_failed")
        inserted = database.execute(
            """INSERT INTO corrupt_export_pages(
                   operation_id,page_number,first_task_id,last_task_id,
                   link_count,page_sha256,rolling_root
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                operation_id,
                page_number,
                links[0]["task_id"],
                links[-1]["task_id"],
                len(links),
                page_sha256,
                rolling_root,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_page_insert_failed")
        state = "manifested" if len(candidates) <= len(links) else "exporting"
        updated = database.execute(
            """UPDATE corrupt_export_operations
               SET cursor_task_id=?,link_count=link_count+?,
                   page_count=page_count+1,rolling_root=?,state=?,updated_at=?
               WHERE operation_id=? AND state='exporting'
                 AND cursor_task_id=? AND page_count=?""",
            (
                links[-1]["task_id"],
                len(links),
                rolling_root,
                state,
                _timestamp(_utc_now()),
                operation_id,
                operation["cursor_task_id"],
                operation["page_count"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_page_fence_lost")

    def _finish_corrupt_export(
        self, database: sqlite3.Connection, operation_id: str
    ) -> None:
        updated = database.execute(
            """UPDATE corrupt_export_operations
               SET state='manifested',updated_at=?
               WHERE operation_id=? AND state='exporting'""",
            (_timestamp(_utc_now()), operation_id),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_manifest_fence_lost")

    def _advance_corrupt_export(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        task_id: str,
        package: Path,
    ) -> None:
        """One bounded step of the resumable lineage export."""
        started = time.monotonic()
        candidates = database.execute(
            """SELECT id,state,created_at,updated_at,input_hash
               FROM tasks WHERE redrive_of=? AND id>?
               ORDER BY id LIMIT 1001""",
            (task_id, operation["cursor_task_id"]),
        ).fetchall()
        links = self._bounded_lineage_links(
            candidates, operation, operation_id, started
        )
        if links:
            self._write_lineage_page(
                database, operation, operation_id, package, links, candidates
            )
            return
        if not candidates:
            self._finish_corrupt_export(database, operation_id)

    def _bounded_purge_children(
        self,
        database: sqlite3.Connection,
        candidates: list,
        operation: sqlite3.Row,
        operation_id: str,
        started: float,
        now: datetime,
    ) -> tuple[list[dict[str, object]], str | None]:
        """As many children as one bounded page holds, and what stopped it."""
        children: list[dict[str, object]] = []
        for child in candidates[:1000]:
            blocker = self._corrupt_child_purge_blocker(database, child, now=now)
            if blocker is not None:
                return children, blocker
            descriptor = self._corrupt_purge_child_descriptor(database, child)
            prospective = canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "page_number": int(operation["page_count"]) + 1,
                    "previous_root": str(operation["rolling_root"]),
                    "before_generation": int(operation["expected_generation"]),
                    "after_generation": int(operation["expected_generation"])
                    + len(children)
                    + 1,
                    "children": [*children, descriptor],
                }
            )
            if len(prospective) > 1024 * 1024 or time.monotonic() - started >= 5:
                return children, None
            children.append(descriptor)
        return children, None

    def _publish_purge_page(self, page_path: Path, page_bytes: bytes) -> bool:
        """False when another owner already wrote a different page here."""
        try:
            _write_durable_file(page_path, page_bytes)
        except QueueOperationError as exc:
            if exc.code != "durable_file_conflict":
                raise
            return False
        try:
            observed_page = _read_stable_owner_file(page_path, 1024 * 1024)
        except (OSError, PermissionError, ValueError) as exc:
            raise QueueOperationError("corrupt_purge_page_invalid") from exc
        if observed_page != page_bytes:
            raise QueueOperationError("corrupt_purge_page_invalid")
        return True

    def _authorize_purge(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        now: datetime,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'corrupt-lineage',?,?,?)""",
            (task_id, operation_id, operation["purge_token"], _timestamp(now)),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("purge_authorization_failed")

    def _clear_purge_authorization(
        self, database: sqlite3.Connection, task_id: str
    ) -> None:
        cleared = database.execute(
            "DELETE FROM task_purge_authorizations WHERE task_id=?",
            (task_id,),
        ).rowcount
        if cleared != 1:
            raise QueueOperationError("purge_authorization_failed")

    def _delete_purged_child(
        self,
        database: sqlite3.Connection,
        child_id: str,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        now: datetime,
    ) -> None:
        self._authorize_purge(database, child_id, operation, operation_id, now)
        self._delete_task_owned_evidence(database, child_id)
        deleted = database.execute(
            "DELETE FROM tasks WHERE id=? AND redrive_of=?",
            (child_id, task_id),
        ).rowcount
        if deleted != 1:
            raise QueueOperationError("corrupt_child_delete_failed")
        self._clear_purge_authorization(database, child_id)

    def _require_parent_generation(
        self, database: sqlite3.Connection, task_id: str, after_generation: int
    ) -> None:
        parent_generation = database.execute(
            "SELECT lineage_generation FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if parent_generation is None:
            raise QueueOperationError("corrupt_lineage_generation_changed")
        if int(parent_generation[0]) != after_generation:
            raise QueueOperationError("corrupt_lineage_generation_changed")

    def _advance_purge_operation(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        children: list[dict[str, object]],
        before_generation: int,
        after_generation: int,
        rolling_root: str,
    ) -> None:
        updated = database.execute(
            """UPDATE corrupt_purge_operations
               SET expected_generation=?,cursor_task_id=?,page_count=page_count+1,
                   rolling_root=?,updated_at=?
               WHERE operation_id=? AND state='purging'
                 AND expected_generation=? AND cursor_task_id=? AND page_count=?""",
            (
                after_generation,
                children[-1]["task_id"],
                rolling_root,
                _timestamp(_utc_now()),
                operation_id,
                before_generation,
                operation["cursor_task_id"],
                operation["page_count"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_purge_page_fence_lost")

    def _commit_purge_page(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        children: list[dict[str, object]],
        package: Path,
        now: datetime,
    ) -> tuple[int, str, bool]:
        """(page number, rolling root, published) for one bounded purge page."""
        page_number = int(operation["page_count"]) + 1
        before_generation = int(operation["expected_generation"])
        after_generation = before_generation + len(children)
        page_bytes = canonical_json_bytes(
            {
                "operation_id": operation_id,
                "page_number": page_number,
                "previous_root": str(operation["rolling_root"]),
                "before_generation": before_generation,
                "after_generation": after_generation,
                "children": children,
            }
        )
        if len(page_bytes) > 1024 * 1024:
            raise QueueOperationError("corrupt_purge_page_too_large")
        page_sha256 = sha256_bytes(page_bytes)
        rolling_root = sha256_bytes(
            bytes.fromhex(str(operation["rolling_root"]))
            + bytes.fromhex(page_sha256)
        )
        page_path = package / f"purge-page-{page_number:08d}.json"
        if not self._publish_purge_page(page_path, page_bytes):
            return page_number, rolling_root, False
        self._insert_corrupt_purge_page(
            database,
            operation_id=operation_id,
            page_number=page_number,
            first_task_id=str(children[0]["task_id"]),
            last_task_id=str(children[-1]["task_id"]),
            deleted_link_count=len(children),
            page_sha256=page_sha256,
            rolling_root=rolling_root,
            expected_generation=after_generation,
        )
        self._authorize_purge(database, task_id, operation, operation_id, now)
        for child in children:
            self._delete_purged_child(
                database, str(child["task_id"]), task_id, operation, operation_id, now
            )
        self._clear_purge_authorization(database, task_id)
        self._require_parent_generation(database, task_id, after_generation)
        self._advance_purge_operation(
            database,
            operation,
            operation_id,
            children,
            before_generation,
            after_generation,
            rolling_root,
        )
        return page_number, rolling_root, True

    def _purge_page_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        task_fence: TaskFence,
    ) -> tuple[sqlite3.Row, int, CorruptPurgeProgress | None]:
        """The operation row and prior link count, or the progress to return."""
        operation = database.execute(
            "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        parent = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if operation is None or parent is None:
            raise QueueOperationError("corrupt_purge_lost")
        prior_links_deleted = int(
            database.execute(
                """SELECT COALESCE(SUM(deleted_link_count),0)
                   FROM corrupt_purge_pages WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()[0]
        )
        pages = int(operation["page_count"])
        if operation["state"] != "purging" or parent["state"] != "purge_pending":
            return (
                operation,
                prior_links_deleted,
                _purge_progress(
                    task_id,
                    operation_id,
                    pages,
                    prior_links_deleted,
                    state="purge_pending",
                ),
            )
        self._require_corrupt_task_fence(database, task_fence)
        if int(parent["lineage_generation"]) != int(operation["expected_generation"]):
            return (
                operation,
                prior_links_deleted,
                _purge_progress(
                    task_id,
                    operation_id,
                    pages,
                    prior_links_deleted,
                    state="blocked",
                    code="corrupt_lineage_generation_changed",
                ),
            )
        return operation, prior_links_deleted, None

    def _purge_corrupt_lineage_page(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        owner: OwnerLease,
        task_fence: TaskFence,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> CorruptPurgeProgress:
        del owner
        _require_active(deadline, cancelled)
        started = time.monotonic()
        with closing(self._connect()) as database, begin_immediate(database):
            operation, prior, early = self._purge_page_preconditions(
                database, task_id, operation_id, task_fence
            )
            if early is not None:
                return early
            pages = int(operation["page_count"])
            candidates = database.execute(
                """SELECT * FROM tasks WHERE redrive_of=? AND id>?
                   ORDER BY id LIMIT 1001""",
                (task_id, operation["cursor_task_id"]),
            ).fetchall()
            if not candidates:
                return _purge_progress(
                    task_id, operation_id, pages, prior, state="purge_pending"
                )
            now = _utc_now()
            children, blocker_code = self._bounded_purge_children(
                database, candidates, operation, operation_id, started, now
            )
            if not children:
                return _purge_progress(
                    task_id,
                    operation_id,
                    pages,
                    prior,
                    state=_blocked_or_pending(blocker_code),
                    code=blocker_code,
                )
            page_number, _root, published = self._commit_purge_page(
                database, task_id, operation, operation_id, children, package, now
            )
            if not published:
                return _purge_progress(
                    task_id,
                    operation_id,
                    pages,
                    prior,
                    state="blocked",
                    code="orphan_corrupt_purge_page_conflict",
                )
            return _purge_progress(
                task_id,
                operation_id,
                page_number,
                prior + len(children),
                state=_blocked_or_pending(blocker_code),
                code=blocker_code,
            )

    @staticmethod
    def _insert_corrupt_purge_page(
        database: sqlite3.Connection,
        *,
        operation_id: str,
        page_number: int,
        first_task_id: str,
        last_task_id: str,
        deleted_link_count: int,
        page_sha256: str,
        rolling_root: str,
        expected_generation: int,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO corrupt_purge_pages(
                   operation_id,page_number,first_task_id,last_task_id,
                   deleted_link_count,page_sha256,rolling_root,expected_generation
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                page_number,
                first_task_id,
                last_task_id,
                deleted_link_count,
                page_sha256,
                rolling_root,
                expected_generation,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_purge_page_insert_failed")

    def _corrupt_child_purge_blocker(
        self,
        database: sqlite3.Connection,
        child: sqlite3.Row,
        *,
        now: datetime,
    ) -> str | None:
        """Why this child of a corrupt export cannot be purged yet."""
        for check in _CORRUPT_CHILD_CHECKS:
            code = check(self, database, child, now)
            if code is not None:
                return code
        return None

    def _corrupt_purge_child_descriptor(
        self, database: sqlite3.Connection, child: sqlite3.Row
    ) -> dict[str, object]:
        task_id = str(child["id"])
        history = canonical_json_bytes(
            [
                {
                    "attempt": int(row["attempt"]),
                    "started_at": str(row["started_at"]),
                    "finished_at": str(row["finished_at"]),
                    "outcome": str(row["outcome"]),
                    "error_code": row["error_code"],
                }
                for row in database.execute(
                    """SELECT attempt,started_at,finished_at,outcome,error_code
                       FROM attempt_history WHERE task_id=? ORDER BY sequence""",
                    (task_id,),
                )
            ]
        )
        task_evidence = canonical_json_bytes(
            {
                "task_id": task_id,
                "state": str(child["state"]),
                "input_hash": str(child["input_hash"]),
                "updated_at": str(child["updated_at"]),
                "lineage_generation": int(child["lineage_generation"]),
            }
        )
        return {
            "task_id": task_id,
            "task_sha256": sha256_bytes(task_evidence),
            "history_sha256": sha256_bytes(history),
        }

    @staticmethod
    def _delete_task_owned_evidence(
        database: sqlite3.Connection, task_id: str
    ) -> None:
        for table in (
            "capture_task_link_seals",
            "capture_task_link_resolutions",
            "capture_task_links",
            "attempt_history",
            "task_source_links",
        ):
            database.execute(f'DELETE FROM "{table}" WHERE task_id=?', (task_id,))

    @contextmanager
    def _corrupt_purge_task_fence(
        self, task_id: str, *, owner: OwnerLease
    ) -> Iterator[TaskFence]:
        fence = self.acquire_task_fence(task_id, mode="queue-operator", owner=owner)
        try:
            yield fence
        finally:
            with closing(self._connect()) as database:
                task = database.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                current = database.execute(
                    """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
                       AND fencing_epoch=?""",
                    (task_id, fence.token, fence.epoch),
                ).fetchone()
            if current is not None:
                self.release_task_fence(fence)
            elif task is not None:
                raise QueueOperationError("task_fence_lost")

    def _capture_terminal_file(
        self, intent_id: str
    ) -> tuple[dict[str, object], bytes]:
        path = self.results_dir / f"capture-{intent_id}.json"
        try:
            raw = _read_stable_owner_file(path, 64 * 1024)
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
            raise QueueOperationError("capture_intent_unresolved") from exc
        if not isinstance(value, dict):
            raise QueueOperationError("capture_terminal_invalid")
        return value, raw

    @staticmethod
    def _require_capture_terminal_identity(
        record: Mapping[str, object], task_id: str, binding: CaptureTaskBinding
    ) -> None:
        processing = record.get("processing_binding")
        disposition = record.get("disposition")
        actual = (
            set(record),
            record.get("schema_version"),
            record.get("intent_id"),
            record.get("intent_sha256"),
            isinstance(record.get("semantic_decisions"), list),
        )
        expected = (
            {
                "schema_version",
                "intent_id",
                "intent_sha256",
                "semantic_decisions",
                "processing_binding",
                "disposition",
            },
            "capture-terminal/v1",
            binding.intent_id,
            binding.intent_sha256,
            True,
        )
        if actual != expected:
            raise QueueOperationError("capture_terminal_invalid")
        expected_processing = {
            "kind": "task",
            "task_id": task_id,
            "active_link_digest": binding.active_digest,
        }
        if processing != expected_processing:
            raise QueueOperationError("capture_terminal_invalid")
        if not isinstance(disposition, dict):
            raise QueueOperationError("capture_terminal_invalid")
        allowed = {"markdown_committed", "no_durable_content", "operator_discard"}
        if disposition.get("kind") not in allowed:
            raise QueueOperationError("capture_terminal_invalid")

    @staticmethod
    def _require_capture_terminal_database_binding(
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
        raw: bytes,
    ) -> None:
        intent = database.execute(
            "SELECT intent_sha256 FROM capture_intents WHERE intent_id=?",
            (binding.intent_id,),
        ).fetchone()
        if intent is None or intent["intent_sha256"] != binding.intent_sha256:
            raise QueueOperationError("capture_terminal_invalid")
        task = database.execute(
            """SELECT result_reference,result_sha256,result_operation_id
               FROM tasks WHERE id=?""",
            (task_id,),
        ).fetchone()
        expected = (
            f"run/queue-results/capture-{binding.intent_id}.json",
            sha256_bytes(raw),
            f"capture-terminal:{binding.intent_id}",
        )
        if task is None or tuple(task) != expected:
            raise QueueOperationError("capture_terminal_unbound")

    def _require_capture_terminal_proof(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> dict[str, object]:
        if binding.intent_id is None or binding.intent_sha256 is None:
            raise QueueOperationError("capture_intent_unresolved")
        record, raw = self._capture_terminal_file(binding.intent_id)
        self._require_capture_terminal_identity(record, task_id, binding)
        self._require_capture_terminal_database_binding(
            database, task_id, binding, raw
        )
        return record

    def _capture_terminal_blocker(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> str | None:
        try:
            self._require_capture_terminal_proof(database, task_id, binding)
        except QueueOperationError as exc:
            return exc.code
        return None

    @staticmethod
    def _require_capture_purge_unshared(
        database: sqlite3.Connection, task_id: str, intent_id: str
    ) -> None:
        other = database.execute(
            """SELECT task_id FROM capture_task_links
               WHERE intent_id=? AND task_id<>?
               UNION ALL
               SELECT task_id FROM capture_task_link_resolutions
               WHERE selected_intent_id=? AND task_id<>?
               LIMIT 1""",
            (intent_id, task_id, intent_id, task_id),
        ).fetchone()
        if other is not None:
            raise QueueOperationError("capture_link_conflicted")

    def _capture_purge_artifact(
        self,
        task_id: str,
        source_path: str,
        expected_sha256: str,
        *,
        max_bytes: int,
        error_code: str,
    ) -> _CapturePurgeArtifact:
        try:
            data = read_runtime_bytes(
                self.state_root / source_path,
                self.state_root,
                max_bytes=max_bytes,
                owner_only=True,
            )
        except (OSError, PermissionError, ValueError) as exc:
            raise QueueOperationError(error_code) from exc
        if sha256_bytes(data) != expected_sha256:
            raise QueueOperationError(error_code)
        archive_path = _capture_purge_archive_path(task_id, source_path)
        return _CapturePurgeArtifact(source_path, archive_path, expected_sha256)

    def _capture_purge_intent_path(
        self, database: sqlite3.Connection, binding: CaptureTaskBinding
    ) -> _CapturePurgeArtifact:
        row = database.execute(
            "SELECT * FROM capture_intents WHERE intent_id=?", (binding.intent_id,)
        ).fetchone()
        if row is None:
            raise QueueOperationError("capture_intent_conflict")
        relative = str(row["relative_path"])
        artifact = self._capture_purge_artifact(
            binding.task_id,
            relative,
            str(binding.intent_sha256),
            max_bytes=_MAX_QUEUE_PAYLOAD_BYTES,
            error_code="capture_intent_conflict",
        )
        size = (self.state_root / relative).stat().st_size
        if (size, row["publication_state"]) != (row["byte_size"], "ready"):
            raise QueueOperationError("capture_intent_conflict")
        return artifact

    def _require_capture_purge_decision_file(
        self, task_id: str, row: sqlite3.Row
    ) -> _CapturePurgeArtifact:
        return self._capture_purge_artifact(
            task_id,
            str(row["decision_path"]),
            str(row["decision_sha256"]),
            max_bytes=1024 * 1024,
            error_code="semantic_decision_conflict",
        )

    @staticmethod
    def _capture_purge_decisions_match(
        rows: list[sqlite3.Row],
        binding: CaptureTaskBinding,
        record: Mapping[str, object],
    ) -> bool:
        indexed = [
            {
                "stage": str(row["stage"]),
                "decision_path": str(row["decision_path"]),
                "decision_sha256": str(row["decision_sha256"]),
            }
            for row in rows
        ]
        digests_match = all(
            row["active_link_digest"] == binding.active_digest for row in rows
        )
        return indexed == record.get("semantic_decisions") and digests_match

    def _capture_purge_decision_paths(
        self,
        database: sqlite3.Connection,
        binding: CaptureTaskBinding,
        record: Mapping[str, object],
    ) -> tuple[_CapturePurgeArtifact, ...]:
        rows = database.execute(
            """SELECT stage,decision_path,decision_sha256,active_link_digest
               FROM semantic_decisions WHERE intent_id=? ORDER BY stage""",
            (binding.intent_id,),
        ).fetchall()
        if not self._capture_purge_decisions_match(rows, binding, record):
            raise QueueOperationError("capture_terminal_invalid")
        return tuple(
            self._require_capture_purge_decision_file(binding.task_id, row)
            for row in rows
        )

    def _capture_purge_evidence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> _CapturePurgeEvidence:
        record = self._require_capture_terminal_proof(database, task_id, binding)
        intent_id = str(binding.intent_id)
        self._require_capture_purge_unshared(database, task_id, intent_id)
        intent = self._capture_purge_intent_path(database, binding)
        decisions = self._capture_purge_decision_paths(
            database, binding, record
        )
        return _CapturePurgeEvidence(
            task_id,
            intent_id,
            intent,
            decisions,
            f"run/queue-results/capture-{intent_id}.json",
        )

    @staticmethod
    def _require_corrupt_task_fence(
        database: sqlite3.Connection, task_fence: TaskFence
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_fence.task_id,
                task_fence.token,
                task_fence.epoch,
                task_fence.owner.token,
                task_fence.owner.epoch,
                _timestamp(_utc_now()),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("task_fence_lost")

    def _publish_corrupt_purge_receipt(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
    ) -> bytes:
        with closing(self._connect()) as database, begin_immediate(database):
            operation = database.execute(
                "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            task = database.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            disposition = database.execute(
                """SELECT disposition.*,export.disposition_key,
                          export.rolling_root AS original_frozen_root
                   FROM corrupt_dispositions AS disposition
                   JOIN corrupt_export_operations AS export
                     ON export.operation_id=disposition.operation_id
                   WHERE disposition.task_id=?""",
                (task_id,),
            ).fetchone()
            if operation is None or task is None or disposition is None:
                raise QueueOperationError("corrupt_purge_lost")
            self._require_corrupt_task_fence(database, task_fence)
            self._require_receipt_preconditions(database, task_id, task, operation)
            _require_package_digests(package, disposition)
            receipt_bytes = _write_purge_receipt(
                package,
                _purge_receipt_record(
                    operation, disposition, operation_id=operation_id, task_id=task_id
                ),
            )
            self._advance_purge_receipt_state(database, operation, operation_id)
            return receipt_bytes

    def _require_receipt_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task: sqlite3.Row,
        operation: sqlite3.Row,
    ) -> None:
        """The task is pending purge, on the expected generation, with no children."""
        incoming_count = int(
            database.execute(
                "SELECT COUNT(*) FROM tasks WHERE redrive_of=?", (task_id,)
            ).fetchone()[0]
        )
        observed = (
            task["state"],
            int(task["lineage_generation"]),
            incoming_count,
        )
        if observed != ("purge_pending", int(operation["expected_generation"]), 0):
            raise QueueOperationError("corrupt_purge_receipt_precondition_failed")

    @staticmethod
    def _advance_purge_receipt_state(
        database: sqlite3.Connection, operation: sqlite3.Row, operation_id: str
    ) -> None:
        """Move the operation to receipt-published, under its own fence."""
        if operation["state"] != "purging":
            return
        updated = database.execute(
            """UPDATE corrupt_purge_operations
               SET state='receipt-published',updated_at=?
               WHERE operation_id=? AND state='purging'
                 AND expected_generation=? AND page_count=? AND rolling_root=?""",
            (
                _timestamp(_utc_now()),
                operation_id,
                operation["expected_generation"],
                operation["page_count"],
                operation["rolling_root"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_purge_receipt_fence_lost")

    def _corrupt_parent_rows(
        self, database: sqlite3.Connection, task_id: str, operation_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        operation = database.execute(
            "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        disposition = database.execute(
            """SELECT disposition.*,export.raw_sha256,export.history_sha256,
                      export.metadata_sha256,export.disposition_key,
                      export.rolling_root AS original_frozen_root
               FROM corrupt_dispositions AS disposition
               JOIN corrupt_export_operations AS export
                 ON export.operation_id=disposition.operation_id
               WHERE disposition.task_id=?""",
            (task_id,),
        ).fetchone()
        if operation is None or task is None or disposition is None:
            raise QueueOperationError("corrupt_purge_lost")
        return operation, task, disposition

    def _current_attempt_history(
        self, database: sqlite3.Connection, task_id: str
    ) -> bytes:
        return canonical_json_bytes(
            [
                {
                    "attempt": int(row["attempt"]),
                    "started_at": str(row["started_at"]),
                    "finished_at": str(row["finished_at"]),
                    "outcome": str(row["outcome"]),
                    "error_code": row["error_code"],
                }
                for row in database.execute(
                    """SELECT attempt,started_at,finished_at,outcome,error_code
                       FROM attempt_history WHERE task_id=? ORDER BY sequence""",
                    (task_id,),
                )
            ]
        )

    def _require_parent_delete_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        operation: sqlite3.Row,
        task: sqlite3.Row,
        disposition: sqlite3.Row,
        package: Path,
    ) -> None:
        """Nothing is deleted until the package still proves what was exported."""
        receipt_bytes = _read_stable_owner_file(
            package / "purge-receipt.json", 64 * 1024
        )
        receipt = json.loads(receipt_bytes.decode("utf-8", errors="strict"))
        validate_schema(
            receipt, Path(__file__).with_name("schemas") / "corrupt-purge-v1.json"
        )
        raw = _read_stable_owner_file(
            package / "payload.bin", _MAX_QUEUE_PAYLOAD_BYTES
        )
        history = _read_stable_owner_file(
            package / "attempt-history.json", _MAX_EXPORT_METADATA_BYTES
        )
        current_history = self._current_attempt_history(database, task_id)
        children = database.execute(
            "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (task_id,)
        ).fetchone()
        if not _parent_state_ready_for_delete(operation, task) or children is not None:
            raise QueueOperationError("corrupt_parent_delete_precondition_failed")
        if not _exported_bytes_match(
            raw, history, current_history, task, disposition
        ):
            raise QueueOperationError("corrupt_parent_delete_precondition_failed")
        if not _receipt_matches_disposition(
            receipt, disposition, operation, operation_id, task_id
        ):
            raise QueueOperationError("corrupt_parent_delete_precondition_failed")

    def _delete_corrupt_parent(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
    ) -> None:
        with closing(self._connect()) as database, begin_immediate(database):
            operation, task, disposition = self._corrupt_parent_rows(
                database, task_id, operation_id
            )
            self._require_corrupt_task_fence(database, task_fence)
            self._require_parent_delete_preconditions(
                database,
                task_id,
                operation_id,
                operation,
                task,
                disposition,
                package,
            )
            inserted = database.execute(
                """INSERT INTO task_purge_authorizations(
                       task_id,mode,operation_id,authorization_digest,created_at
                   ) VALUES (?,'corrupt-parent',?,?,?)""",
                (
                    task_id,
                    operation_id,
                    operation["purge_token"],
                    _timestamp(_utc_now()),
                ),
            ).rowcount
            if inserted != 1:
                raise QueueOperationError("purge_authorization_failed")
            deleted_fence = database.execute(
                """DELETE FROM task_fences WHERE task_id=? AND token=?
                   AND fencing_epoch=?""",
                (task_id, task_fence.token, task_fence.epoch),
            ).rowcount
            if deleted_fence != 1:
                raise QueueOperationError("task_fence_lost")
            self._delete_task_owned_evidence(database, task_id)
            deleted_task = database.execute(
                "DELETE FROM tasks WHERE id=?", (task_id,)
            ).rowcount
            if deleted_task != 1:
                raise QueueOperationError("corrupt_parent_delete_failed")
            self._clear_purge_authorization(database, task_id)

    def _corrupt_purge_rows(
        self, database: sqlite3.Connection, task_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row | None]:
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        disposition = database.execute(
            """SELECT disposition.*, operation.disposition_key,
                      operation.rolling_root AS original_frozen_root,
                      operation.lineage_generation
               FROM corrupt_dispositions AS disposition
               JOIN corrupt_export_operations AS operation
                 ON operation.operation_id=disposition.operation_id
               WHERE disposition.task_id=?""",
            (task_id,),
        ).fetchone()
        if task is None or disposition is None:
            raise QueueOperationError("corrupt_purge_state_invalid")
        if task["state"] not in {"quarantined", "purge_pending"}:
            raise QueueOperationError("corrupt_purge_state_invalid")
        existing_operation = database.execute(
            "SELECT * FROM corrupt_purge_operations WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return task, disposition, existing_operation

    def _retention_blocker(
        self,
        disposition: sqlite3.Row,
        existing_operation: sqlite3.Row | None,
        now: datetime,
    ) -> str | None:
        """A disposed task is retained for thirty days before it may be purged."""
        disposed_at = _parse_timestamp(str(disposition["disposed_at"]))
        if disposed_at is None:
            raise QueueOperationError("corrupt_disposition_invalid")
        if existing_operation is not None:
            return None
        if disposed_at + timedelta(days=30) > now:
            return "corrupt_retention_active"
        return None

    def _start_corrupt_purge(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task: sqlite3.Row,
        operation_id: str,
        task_fence: TaskFence,
        now: datetime,
    ) -> str | None:
        """None once the operation exists, or the code that blocks starting it."""
        try:
            binding = self.active_capture_binding(database, task_id)
        except QueueOperationError:
            return "capture_intent_unresolved"
        terminal_code = self._capture_terminal_blocker(database, task_id, binding)
        if terminal_code is not None:
            return terminal_code
        purge_token = sha256_bytes(
            canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "task_id": task_id,
                    "task_fence_epoch": task_fence.epoch,
                }
            )
        )
        inserted = database.execute(
            """INSERT INTO corrupt_purge_operations(
                   operation_id,task_id,purge_token,expected_generation,
                   cursor_task_id,page_count,rolling_root,state,created_at,updated_at
               ) VALUES (?,?,?,?,'',0,?,'purging',?,?)""",
            (
                operation_id,
                task_id,
                purge_token,
                int(task["lineage_generation"]),
                sha256_bytes(b""),
                _timestamp(now),
                _timestamp(now),
            ),
        ).rowcount
        changed = database.execute(
            """UPDATE tasks SET state='purge_pending',updated_at=?
               WHERE id=? AND state='quarantined'""",
            (_timestamp(now), task_id),
        ).rowcount
        if (inserted, changed) != (1, 1):
            raise QueueOperationError("corrupt_purge_start_failed")
        return None

    def _directed_corrupt_purge(
        self, task_id: str, task_fence: TaskFence, now: datetime
    ) -> tuple[str, Path, str | None]:
        """(operation id, package, blocking code) for this purge attempt."""
        with closing(self._connect()) as database, begin_immediate(database):
            task, disposition, existing_operation = self._corrupt_purge_rows(
                database, task_id
            )
            retention = self._retention_blocker(disposition, existing_operation, now)
            if retention is not None:
                return "", self.results_dir, retention
            package_key = str(disposition["disposition_key"])
            package = self.results_dir / f"corrupt-{package_key}"
            package_code = self._corrupt_package_purge_blocker(package, disposition)
            if package_code is not None:
                return "", package, package_code
            operation_key = sha256_bytes(
                canonical_json_bytes(
                    {
                        "disposition_sha256": disposition["disposition_sha256"],
                        "package_key": package_key,
                        "task_id": task_id,
                    }
                )
            )
            operation_id = f"corrupt-purge:{operation_key}"
            if existing_operation is None:
                blocker = self._start_corrupt_purge(
                    database, task_id, task, operation_id, task_fence, now
                )
                return operation_id, package, blocker
            _require_matching_purge_operation(existing_operation, task, operation_id)
            return operation_id, package, None

    def _finish_corrupt_purge(
        self,
        task_id: str,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
        owner: OwnerLease,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> CorruptPurgeProgress:
        page_progress = self._purge_corrupt_lineage_page(
            task_id,
            operation_id=operation_id,
            package=package,
            owner=owner,
            task_fence=task_fence,
            deadline=deadline,
            cancelled=cancelled,
        )
        if page_progress.state == "blocked":
            return page_progress
        with closing(self._connect()) as database:
            incoming = database.execute(
                "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (task_id,)
            ).fetchone()
        if incoming is not None:
            return page_progress
        self._publish_corrupt_purge_receipt(
            task_id,
            operation_id=operation_id,
            package=package,
            task_fence=task_fence,
        )
        self._delete_corrupt_parent(
            task_id,
            operation_id=operation_id,
            package=package,
            task_fence=task_fence,
        )
        return CorruptPurgeProgress(
            task_id=task_id,
            operation_id=operation_id,
            state="purged",
            pages_written=page_progress.pages_written,
            links_deleted=page_progress.links_deleted,
            complete=True,
            code=None,
        )

    def purge_quarantined(
        self,
        task_id: str,
        *,
        owner: OwnerLease,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> CorruptPurgeProgress:
        from operational_ownership import OwnerLease

        _require_active(deadline, cancelled)
        _require_repair_owner(owner, OwnerLease, "corrupt purge requires a repair owner")
        _require_task_identifier(task_id)
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        completed = self._completed_corrupt_purge_progress(task_id)
        if completed is not None:
            return completed
        with self.queue_owner(
            role="queue-operator",
            scope=f"task:{sha256_bytes(task_id.encode('utf-8'))}",
            parent=owner,
        ), self._corrupt_purge_task_fence(task_id, owner=owner) as task_fence:
            operation_id, package, blocker = self._directed_corrupt_purge(
                task_id, task_fence, _utc_now()
            )
            if blocker is not None:
                return _blocked_purge(task_id, blocker)
            return self._finish_corrupt_purge(
                task_id, operation_id, package, task_fence, owner, deadline, cancelled
            )

    def _completed_corrupt_purge_progress(
        self, task_id: str
    ) -> CorruptPurgeProgress | None:
        completed = self._completed_purge_rows(task_id)
        if completed is None:
            return None
        operation, disposition, links_deleted = completed
        package = self.results_dir / f"corrupt-{disposition['disposition_key']}"
        receipt = _read_purge_receipt(package)
        _require_matching_purge_receipt(receipt, operation, disposition, task_id)
        return CorruptPurgeProgress(
            task_id=task_id,
            operation_id=str(operation["operation_id"]),
            state="purged",
            pages_written=int(operation["page_count"]),
            links_deleted=links_deleted,
            complete=True,
            code=None,
        )

    def _completed_purge_rows(
        self, task_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, int] | None:
        """The purge rows for a task that is gone, or None while it is still here."""
        with closing(self._connect()) as database:
            task_exists = database.execute(
                "SELECT 1 FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            operation = database.execute(
                "SELECT * FROM corrupt_purge_operations WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task_exists is not None or operation is None:
                return None
            if operation["state"] != "receipt-published":
                raise QueueOperationError("corrupt_purge_completion_invalid")
            disposition = database.execute(
                """SELECT disposition.*,export.disposition_key
                   FROM corrupt_dispositions AS disposition
                   JOIN corrupt_export_operations AS export
                     ON export.operation_id=disposition.operation_id
                   WHERE disposition.task_id=?""",
                (task_id,),
            ).fetchone()
            links_deleted = int(
                database.execute(
                    """SELECT COALESCE(SUM(deleted_link_count),0)
                       FROM corrupt_purge_pages WHERE operation_id=?""",
                    (operation["operation_id"],),
                ).fetchone()[0]
            )
        if disposition is None:
            raise QueueOperationError("corrupt_purge_completion_invalid")
        return operation, disposition, links_deleted

    def _corrupt_package_purge_blocker(
        self, package: Path, disposition: sqlite3.Row
    ) -> str | None:
        """Why the exported package cannot be trusted as the task's evidence."""
        try:
            if not _owner_only_directory(package):
                return "corrupt_package_invalid"
            records = _validated_corrupt_records(package)
            if not _corrupt_records_match(*records, disposition):
                return "corrupt_package_invalid"
            if not _corrupt_payload_matches(package, records[2], disposition):
                return "corrupt_package_invalid"
        except (OSError, PermissionError, ValueError, json.JSONDecodeError):
            return "corrupt_package_invalid"
        return None

    @contextmanager
    def queue_owner(
        self,
        *,
        role: Literal["queue-worker", "queue-operator"],
        scope: str,
        parent: OwnerLease | None = None,
    ) -> Iterator[OwnerLease]:
        from operational_ownership import OwnerLease

        allowed = {
            "queue-worker": {"queue-worker", "compile", "doctor", "nightly", "weekly"},
            "queue-operator": {"queue-operator", "repair"},
        }
        if role not in allowed:
            raise ValueError("queue owner role must be queue-worker or queue-operator")
        registry = self.ownership_registry()
        nested = parent is not None
        if nested:
            if not isinstance(parent, OwnerLease):
                raise TypeError("parent must be an OwnerLease")
            if parent.role not in allowed[role]:
                raise ValueError("parent role cannot project the requested queue role")
            with closing(registry._connect()) as database:
                registry.require(database, parent)
            lease = parent
        else:
            lease = registry.acquire(role, scope=scope)
        try:
            self._insert_queue_projection(lease, role=role, scope=scope)
        except BaseException:
            if not nested:
                registry.release(lease)
            raise
        try:
            yield lease
        finally:
            self._remove_queue_projection(lease)
            if not nested:
                registry.release(lease)

    def _insert_queue_projection(
        self,
        lease: OwnerLease,
        *,
        role: Literal["queue-worker", "queue-operator"],
        scope: str,
    ) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(lease, OwnerLease):
            raise TypeError("lease must be an OwnerLease")
        domain_role = "worker" if role == "queue-worker" else "operator"
        with closing(
            open_operational_db(
                self.db_path,
                busy_ms=DEFAULTS.queue_busy_ms,
                contract=_QUEUE_V3_CONTRACT,
            )
        ) as database, begin_immediate(database):
            database.execute(
                """INSERT INTO queue_ownership(
                       actor_id,domain_role,canonical_role,canonical_scope,
                       owner_token,fencing_epoch,process_id,process_start_identity,
                       acquired_at,heartbeat_at,expires_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lease.actor_id,
                    domain_role,
                    lease.role,
                    lease.scope,
                    lease.token,
                    lease.epoch,
                    lease.process.pid,
                    lease.process.start_identity,
                    _timestamp(lease.acquired_at),
                    _timestamp(lease.heartbeat_at),
                    _timestamp(lease.expires_at),
                ),
            )

    def heartbeat_queue_owner(self, lease: OwnerLease) -> None:
        registry = self.ownership_registry()
        current = registry.heartbeat(lease)
        with closing(
            open_operational_db(
                self.db_path,
                busy_ms=DEFAULTS.queue_busy_ms,
                contract=_QUEUE_V3_CONTRACT,
            )
        ) as database, begin_immediate(database):
            updated = database.execute(
                """UPDATE queue_ownership SET heartbeat_at=?,expires_at=?
                   WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                     AND owner_token=? AND fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    _timestamp(current.heartbeat_at),
                    _timestamp(current.expires_at),
                    current.actor_id,
                    current.role,
                    current.scope,
                    current.token,
                    current.epoch,
                    current.process.pid,
                    current.process.start_identity,
                ),
            ).rowcount
            if updated != 1:
                raise QueueOperationError("queue_owner_fence_lost")

    def _remove_queue_projection(self, lease: OwnerLease) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(lease, OwnerLease):
            raise TypeError("lease must be an OwnerLease")
        with closing(
            open_operational_db(
                self.db_path,
                busy_ms=DEFAULTS.queue_busy_ms,
                contract=_QUEUE_V3_CONTRACT,
            )
        ) as database, begin_immediate(database):
            deleted = database.execute(
                """DELETE FROM queue_ownership
                   WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                     AND owner_token=? AND fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    lease.actor_id,
                    lease.role,
                    lease.scope,
                    lease.token,
                    lease.epoch,
                    lease.process.pid,
                    lease.process.start_identity,
                ),
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("queue_owner_fence_lost")

    def claim(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int = DEFAULTS.queue_max_attempts,
    ) -> QueueLease | None:
        if not owner:
            raise ValueError("owner must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        _validate_retry_policy(
            max_attempts, DEFAULTS.retry_base_seconds, DEFAULTS.retry_cap_seconds
        )
        now = _utc_now()
        with closing(
            self._connect()
        ) as database, begin_immediate(database):
            while True:
                row = database.execute(
                    """SELECT * FROM tasks
                       WHERE state='ready' AND attempts < ? AND available_at <= ?
                       ORDER BY priority DESC, available_at, created_at, id LIMIT 1""",
                    (max_attempts, _timestamp(now)),
                ).fetchone()
                if row is None:
                    return None
                validation = self._require_valid_task_payload(
                    database, row, now=now, parse=True
                )
                if validation is None:
                    continue
                history_count = database.execute(
                    "SELECT COUNT(*) FROM attempt_history WHERE task_id=?",
                    (row["id"],),
                ).fetchone()[0]
                if int(history_count) >= _MAX_RUNTIME_ATTEMPTS:
                    changed = database.execute(
                        """UPDATE tasks SET state='dead',
                               error_code='attempt_history_exhausted',updated_at=?
                           WHERE id=? AND state='ready'""",
                        (_timestamp(now), row["id"]),
                    ).rowcount
                    if changed != 1:
                        raise QueueOperationError("attempt_history_demotion_failed")
                    continue
                break
            token = sha256_bytes(os.urandom(32))
            expires_at = now + timedelta(seconds=lease_seconds)
            changed = database.execute(
                """UPDATE tasks SET state='leased', attempts=attempts+1,
                       lease_owner=?, lease_token=?, lease_expires_at=?,
                       lease_heartbeat_at=?, attempt_started_at=?, updated_at=?
                   WHERE id=? AND state='ready' AND attempts < ?""",
                (
                    owner,
                    token,
                    _timestamp(expires_at),
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    row["id"],
                    max_attempts,
                ),
            ).rowcount
            if changed != 1:
                return None
        return QueueLease(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=validation.payload or {},
            input_hash=str(row["input_hash"]),
            owner=owner,
            token=token,
            expires_at=expires_at,
            attempt=int(row["attempts"]) + 1,
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    @staticmethod
    def _validate_capture_claim(
        owner: str, lease_seconds: int, max_attempts: int
    ) -> None:
        if not owner:
            raise ValueError("owner must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        _validate_retry_policy(
            max_attempts, DEFAULTS.retry_base_seconds, DEFAULTS.retry_cap_seconds
        )

    @staticmethod
    def _capture_claim_row(
        database: sqlite3.Connection, max_attempts: int, now: datetime
    ) -> sqlite3.Row | None:
        return database.execute(
            """SELECT task.* FROM tasks AS task
               JOIN capture_task_links AS link ON link.task_id=task.id
               WHERE task.state='ready' AND task.kind='flush'
                 AND task.handler_version=1 AND task.attempts<?
                 AND task.available_at<=?
                 AND (SELECT COUNT(*) FROM attempt_history AS history
                      WHERE history.task_id=task.id)<?
               ORDER BY task.priority DESC,task.available_at,task.created_at,task.id
               LIMIT 1""",
            (max_attempts, _timestamp(now), _MAX_RUNTIME_ATTEMPTS),
        ).fetchone()

    def _lease_capture_row(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        owner: str,
        lease_seconds: int,
        max_attempts: int,
        now: datetime,
    ) -> QueueLease | None:
        validation = self._require_valid_task_payload(database, row, now=now, parse=True)
        if validation is None:
            return None
        token = sha256_bytes(os.urandom(32))
        expires_at = now + timedelta(seconds=lease_seconds)
        changed = database.execute(
            """UPDATE tasks SET state='leased',attempts=attempts+1,
                   lease_owner=?,lease_token=?,lease_expires_at=?,
                   lease_heartbeat_at=?,attempt_started_at=?,updated_at=?
               WHERE id=? AND state='ready' AND attempts<?""",
            (
                owner,
                token,
                _timestamp(expires_at),
                _timestamp(now),
                _timestamp(now),
                _timestamp(now),
                row["id"],
                max_attempts,
            ),
        ).rowcount
        if changed != 1:
            return None
        return QueueLease(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=validation.payload or {},
            input_hash=str(row["input_hash"]),
            owner=owner,
            token=token,
            expires_at=expires_at,
            attempt=int(row["attempts"]) + 1,
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    def claim_capture(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int = DEFAULTS.queue_max_attempts,
    ) -> QueueLease | None:
        self._validate_capture_claim(owner, lease_seconds, max_attempts)
        now = _utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._capture_claim_row(database, max_attempts, now)
            if row is None:
                return None
            return self._lease_capture_row(
                database,
                row,
                owner=owner,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
                now=now,
            )

    def heartbeat(
        self,
        lease: QueueLease,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> QueueLease:
        if lease_seconds <= 0:
            raise ValueError("lease must be positive")
        now = _utc_now()
        expires_at = now + timedelta(seconds=lease_seconds)
        mismatch = False
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            validation = self._require_valid_task_payload(
                database, row, now=now, parse=True
            )
            mismatch = validation is None
            if not mismatch:
                changed = database.execute(
                    """UPDATE tasks SET lease_expires_at=?, lease_heartbeat_at=?,
                           updated_at=? WHERE id=? AND lease_token=? AND state='leased'""",
                    (
                        _timestamp(expires_at),
                        _timestamp(now),
                        _timestamp(now),
                        lease.id,
                        lease.token,
                    ),
                ).rowcount
                if changed != 1:
                    raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        if mismatch:
            self._raise_payload_mismatch()
        return replace(lease, expires_at=expires_at)

    def payload_for_execution(self, lease: QueueLease) -> dict[str, object]:
        now = _utc_now()
        payload: dict[str, object] | None = None
        mismatch = False
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            validation = self._require_valid_task_payload(
                database, row, now=now, parse=True
            )
            mismatch = validation is None
            if validation is not None:
                payload = validation.payload
        if mismatch:
            self._raise_payload_mismatch()
        return payload or {}

    def _validated_result_digest(self, relative: str) -> str | None:
        try:
            path = self.state_root / relative
            if path.parent.resolve(strict=True) != self.results_dir.resolve(strict=True):
                return None
            data = _read_stable_owner_file(path, _MAX_RESULT_BYTES)
            return sha256_bytes(data)
        except (OSError, PermissionError, ValueError):
            return None

    def adopt_published_result(
        self, lease: QueueLease, *, operation_id: str
    ) -> str | None:
        if not operation_id:
            raise ValueError("operation_id must be non-empty")
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        if not (self.state_root / relative).exists():
            return None
        now = _utc_now()
        mismatch = False
        outcome: str | None = None
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            validation = self._require_valid_task_payload(
                database, row, now=now, parse=True
            )
            mismatch = validation is None
            if not mismatch:
                digest = self._validated_result_digest(relative)
                if digest is None:
                    outcome = "corrupt"
                    database.execute(
                        """UPDATE tasks SET state='dead',error_code='result_corrupt',
                               updated_at=?,lease_owner=NULL,lease_token=NULL,
                               lease_expires_at=NULL,lease_heartbeat_at=NULL,
                               attempt_started_at=NULL WHERE id=?""",
                        (_timestamp(now), lease.id),
                    )
                else:
                    changed = database.execute(
                        """UPDATE tasks SET result_reference=?,result_sha256=?,
                               result_operation_id=?,updated_at=?
                           WHERE id=? AND lease_token=? AND state='leased'""",
                        (
                            relative,
                            digest,
                            operation_id,
                            _timestamp(now),
                            lease.id,
                            lease.token,
                        ),
                    ).rowcount
                    if changed != 1:
                        raise LeaseFenceError(
                            f"lease is stale or not owned: {lease.id}"
                        )
                    outcome = "adopted"
        if mismatch:
            self._raise_payload_mismatch()
        return outcome

    def publish_result(
        self, lease: QueueLease, *, operation_id: str, result: bytes
    ) -> str:
        if not operation_id:
            raise ValueError("operation_id must be non-empty")
        if not isinstance(result, bytes):
            raise TypeError("result must be bytes")
        if len(result) > _MAX_RESULT_BYTES:
            raise ValueError("result exceeds maximum queue result size")
        now = _utc_now()
        mismatch = False
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            mismatch = (
                self._require_valid_task_payload(
                    database, row, now=now, parse=True
                )
                is None
            )
        if mismatch:
            self._raise_payload_mismatch()
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.results_dir, 0o700)
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        target = self.results_dir / result_name
        _write_durable_file(target, result)
        relative = f"run/queue-results/{result_name}"
        digest = sha256_bytes(result)
        mismatch = False
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, _utc_now())
            mismatch = (
                self._require_valid_task_payload(
                    database, row, now=_utc_now(), parse=True
                )
                is None
            )
            if not mismatch:
                existing_operation = row["result_operation_id"]
                if existing_operation not in {None, operation_id}:
                    raise ResultConflictError(
                        "lease already published a different operation"
                    )
                existing_digest = self._validated_result_digest(relative)
                if existing_digest != digest:
                    raise ResultConflictError(
                        "operation ID already has different result bytes"
                    )
                changed = database.execute(
                    """UPDATE tasks SET result_reference=?,result_sha256=?,
                           result_operation_id=?,updated_at=?
                       WHERE id=? AND lease_token=? AND state='leased'""",
                    (
                        relative,
                        digest,
                        operation_id,
                        _timestamp(_utc_now()),
                        lease.id,
                        lease.token,
                    ),
                ).rowcount
                if changed != 1:
                    raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        if mismatch:
            self._raise_payload_mismatch()
        return relative

    def acknowledge(self, lease: QueueLease) -> None:
        now = _utc_now()
        mismatch = False
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            mismatch = (
                self._require_valid_task_payload(
                    database, row, now=now, parse=True
                )
                is None
            )
            if not mismatch:
                digest = row["result_sha256"]
                reference = row["result_reference"]
                valid_result = (
                    isinstance(digest, str)
                    and isinstance(reference, str)
                    and self._validated_result_digest(reference) == digest
                )
                state = "succeeded" if valid_result else "dead"
                error_code = None if valid_result else "result_corrupt"
                database.execute(
                    """INSERT INTO attempt_history(
                           task_id,attempt,started_at,finished_at,outcome,error_code
                       ) VALUES (?,?,?,?,?,?)""",
                    (
                        lease.id,
                        row["attempts"],
                        row["attempt_started_at"] or _timestamp(now),
                        _timestamp(now),
                        "succeeded" if valid_result else "failed",
                        error_code,
                    ),
                )
                changed = database.execute(
                    """UPDATE tasks SET state=?,error_code=?,updated_at=?,
                           lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                           lease_heartbeat_at=NULL,attempt_started_at=NULL
                       WHERE id=? AND lease_token=? AND state='leased'""",
                    (state, error_code, _timestamp(now), lease.id, lease.token),
                ).rowcount
                if changed != 1:
                    raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        if mismatch:
            self._raise_payload_mismatch()

    def fail(self, lease: QueueLease, failure: QueueFailure) -> None:
        if not isinstance(failure, QueueFailure) or not failure.error_code:
            raise ValueError("failure must have a non-empty error code")
        now = _utc_now()
        mismatch = False
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            mismatch = (
                self._require_valid_task_payload(
                    database, row, now=now, parse=True
                )
                is None
            )
            if not mismatch:
                outcome = "blocked" if failure.blocked_capability else "failed"
                database.execute(
                    """INSERT INTO attempt_history(
                           task_id,attempt,started_at,finished_at,outcome,error_code
                       ) VALUES (?,?,?,?,?,?)""",
                    (
                        lease.id,
                        row["attempts"],
                        row["attempt_started_at"] or _timestamp(now),
                        _timestamp(now),
                        outcome,
                        failure.error_code,
                    ),
                )
                if failure.blocked_capability:
                    state = "blocked"
                    attempts = int(row["attempts"]) - 1
                elif failure.permanent or failure.error_code in _PERMANENT_CODES:
                    state = "dead"
                    attempts = int(row["attempts"])
                else:
                    state = "ready"
                    attempts = int(row["attempts"])
                changed = database.execute(
                    """UPDATE tasks SET state=?,attempts=?,error_code=?,
                           blocked_capability=?,updated_at=?,available_at=?,
                           lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                           lease_heartbeat_at=NULL,attempt_started_at=NULL
                       WHERE id=? AND lease_token=? AND state='leased'""",
                    (
                        state,
                        attempts,
                        failure.error_code,
                        failure.blocked_capability,
                        _timestamp(now),
                        _timestamp(now),
                        lease.id,
                        lease.token,
                    ),
                ).rowcount
                if changed != 1:
                    raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        if mismatch:
            self._raise_payload_mismatch()

    def recover_expired_leases(self) -> int:
        now = _utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            rows = database.execute(
                "SELECT * FROM tasks WHERE state='leased' AND lease_expires_at<=?",
                (_timestamp(now),),
            ).fetchall()
            for row in rows:
                validation = self._require_valid_task_payload(
                    database, row, now=now, parse=True
                )
                if validation is None:
                    continue
                database.execute(
                    """INSERT INTO attempt_history(
                           task_id,attempt,started_at,finished_at,outcome,error_code
                       ) VALUES (?,?,?,?,?,?)""",
                    (
                        row["id"],
                        row["attempts"],
                        row["attempt_started_at"] or _timestamp(now),
                        _timestamp(now),
                        "lease_expired",
                        "lease_expired",
                    ),
                )
                changed = database.execute(
                    """UPDATE tasks SET state='ready',error_code='lease_expired',
                           updated_at=?,available_at=?,lease_owner=NULL,lease_token=NULL,
                           lease_expires_at=NULL,lease_heartbeat_at=NULL,
                           attempt_started_at=NULL WHERE id=? AND state='leased'""",
                    (_timestamp(now), _timestamp(now), row["id"]),
                ).rowcount
                if changed != 1:
                    raise QueueOperationError("lease_expiry_failed")
        return len(rows)

    def cancel(self, task_id: str) -> bool:
        now = _utc_now()
        mismatch = False
        changed = False
        with closing(self._connect()) as database, begin_immediate(database):
            row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in _TERMINAL_STATES:
                return False
            mismatch = (
                self._require_valid_task_payload(
                    database, row, now=now, parse=True
                )
                is None
            )
            if not mismatch:
                changed = database.execute(
                    """UPDATE tasks SET state='cancelled',error_code='cancelled',
                           updated_at=?,lease_owner=NULL,lease_token=NULL,
                           lease_expires_at=NULL,lease_heartbeat_at=NULL,
                           attempt_started_at=NULL WHERE id=? AND state=?""",
                    (_timestamp(now), task_id, row["state"]),
                ).rowcount == 1
        if mismatch:
            self._raise_payload_mismatch()
        return changed

    def redrive(self, task_id: str) -> str:
        now = _utc_now()
        mismatch = False
        replacement = uuid.uuid4().hex
        with closing(self._connect()) as database, begin_immediate(database):
            row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] != "dead":
                raise QueueOperationError("redrive_requires_dead")
            validation = self._require_valid_task_payload(
                database, row, now=now, parse=True
            )
            mismatch = validation is None
            if not mismatch:
                inserted = database.execute(
                    """INSERT INTO tasks(
                           id,kind,handler_version,payload_blob,input_hash,state,
                           priority,created_at,updated_at,available_at,redrive_of
                       ) VALUES (?,?,?,?,?,'ready',?,?,?,?,?)""",
                    (
                        replacement,
                        row["kind"],
                        row["handler_version"],
                        validation.raw,
                        validation.input_hash,
                        row["priority"],
                        _timestamp(now),
                        _timestamp(now),
                        _timestamp(now),
                        task_id,
                    ),
                ).rowcount
                if inserted != 1:
                    raise QueueOperationError("redrive_failed")
        if mismatch:
            self._raise_payload_mismatch()
        return replacement

    def export_task(self, task_id: str) -> dict[str, object]:
        with closing(self._connect()) as database:
            row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            validation = validate_payload_blob(
                bytes(row["payload_blob"]), row["input_hash"], parse=True
            )
            if validation.code is not None:
                raise QueueOperationError("payload_hash_mismatch")
            history = database.execute(
                """SELECT attempt,started_at,finished_at,outcome,error_code
                   FROM attempt_history WHERE task_id=? ORDER BY sequence""",
                (task_id,),
            ).fetchall()
            source_links = database.execute(
                """SELECT logical_path,source_digest FROM task_source_links
                   WHERE task_id=? ORDER BY logical_path,source_digest""",
                (task_id,),
            ).fetchall()
            link = database.execute(
                "SELECT * FROM capture_task_links WHERE task_id=?", (task_id,)
            ).fetchone()
            seal = database.execute(
                "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
            ).fetchone()
        lease_values = (
            row["lease_owner"],
            row["lease_token"],
            row["lease_expires_at"],
            row["lease_heartbeat_at"],
        )
        lease = (
            None
            if lease_values == (None, None, None, None)
            else {
                "owner": lease_values[0],
                "token": lease_values[1],
                "expires_at": lease_values[2],
                "heartbeat_at": lease_values[3],
            }
        )
        result_values = (
            row["result_reference"],
            row["result_sha256"],
            row["result_operation_id"],
        )
        result = (
            None
            if result_values == (None, None, None)
            else {
                "reference": result_values[0],
                "sha256": result_values[1],
                "operation_id": result_values[2],
            }
        )
        capture_binding = None
        if link is not None:
            active_digest = str(link["link_digest"])
            capture_binding = {
                "intent_id": link["intent_id"],
                "intent_sha256": link["intent_sha256"],
                "handler_version": int(link["handler_version"]),
                "active_digest": active_digest,
                "seal_digest": None if seal is None else seal["seal_digest"],
            }
        record: dict[str, object] = {
            "schema_version": "queue-task/v3",
            "task_id": str(row["id"]),
            "kind": str(row["kind"]),
            "handler_version": int(row["handler_version"]),
            "payload": validation.payload,
            "input_hash": str(row["input_hash"]),
            "dedupe_key": row["dedupe_key"],
            "state": str(row["state"]),
            "priority": int(row["priority"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "available_at": str(row["available_at"]),
            "attempts": int(row["attempts"]),
            "last_attempt_at": row["last_attempt_at"],
            "lease": lease,
            "error_code": row["error_code"],
            "blocked_capability": row["blocked_capability"],
            "result": result,
            "redrive_of": row["redrive_of"],
            "lineage_generation": int(row["lineage_generation"]),
            "attempt_history": [
                {
                    "attempt": int(item["attempt"]),
                    "started_at": str(item["started_at"]),
                    "finished_at": str(item["finished_at"]),
                    "outcome": str(item["outcome"]),
                    "error_code": item["error_code"],
                }
                for item in history
            ],
            "source_links": [
                {
                    "logical_path": str(item["logical_path"]),
                    "source_digest": str(item["source_digest"]),
                }
                for item in source_links
            ],
            "capture_binding": capture_binding,
        }
        validate_schema(
            record, Path(__file__).with_name("schemas") / "queue-task-v3.json"
        )
        if len(canonical_json_bytes(record)) > _MAX_EXPORT_METADATA_BYTES:
            raise QueueOperationError("export_metadata_too_large")
        return record

    def purge(
        self,
        *,
        terminal_before: datetime,
        export_path: Path,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> PurgeReceipt:
        _require_active(deadline, cancelled)
        plan = self._ordinary_purge_plan(terminal_before, export_path)
        manifest_bytes = self._publish_ordinary_purge_export(
            plan, deadline=deadline, cancelled=cancelled
        )
        capture_evidence = self._commit_ordinary_purge(
            plan,
            manifest_bytes,
            deadline=deadline,
            cancelled=cancelled,
        )
        self._cleanup_ordinary_purge_artifacts(plan, capture_evidence)
        self._publish_ordinary_purge_receipt(plan, manifest_bytes)
        self._clear_ordinary_purge_authorizations(plan, manifest_bytes)
        return PurgeReceipt(len(plan.task_ids), plan.task_ids)

    def _ordinary_purge_plan(
        self, terminal_before: datetime, export_path: Path
    ) -> _OrdinaryPurgePlan:
        retention_cutoff = _utc_now() - timedelta(
            days=DEFAULTS.queue_result_retention_days
        )
        cutoff = _timestamp(min(_as_utc(terminal_before), retention_cutoff))
        export = Path(export_path).absolute()
        if export.is_symlink():
            raise QueueOperationError("export_verification_failed")
        if export.exists():
            return self._load_ordinary_purge_plan(cutoff, export)
        return self._new_ordinary_purge_plan(cutoff, export)

    def _new_ordinary_purge_plan(
        self, cutoff: str, export: Path
    ) -> _OrdinaryPurgePlan:
        with closing(self._connect()) as database:
            database.execute("BEGIN")
            rows = database.execute(
                """SELECT * FROM tasks
                   WHERE state IN ('succeeded','cancelled') AND updated_at<?
                   ORDER BY created_at,id""",
                (cutoff,),
            ).fetchall()
            records = tuple(
                self._export_task_in_transaction(database, row) for row in rows
            )
            capture_evidence = self._ordinary_capture_evidence_for_rows(
                database, rows
            )
        task_ids = tuple(str(row["id"]) for row in rows)
        records_bytes = canonical_json_bytes(list(records))
        if len(records_bytes) > _MAX_EXPORT_METADATA_BYTES:
            raise QueueOperationError("export_metadata_too_large")
        return _OrdinaryPurgePlan(
            cutoff,
            export,
            task_ids,
            records,
            records_bytes,
            capture_evidence,
            None,
        )

    def _ordinary_capture_evidence_for_rows(
        self, database: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> tuple[_CapturePurgeEvidence, ...]:
        evidence: list[_CapturePurgeEvidence] = []
        for row in rows:
            evidence.extend(
                self._ordinary_capture_purge_evidence(database, str(row["id"]))
            )
        return tuple(evidence)

    def _load_ordinary_purge_plan(
        self, cutoff: str, export: Path
    ) -> _OrdinaryPurgePlan:
        self._require_ordinary_purge_export_directory(export)
        try:
            records_bytes = _read_stable_owner_file(
                export / "records.json", _MAX_EXPORT_METADATA_BYTES
            )
            manifest_bytes = _read_stable_owner_file(
                export / "manifest.json", _MAX_EXPORT_METADATA_BYTES
            )
            records, manifest = self._decode_ordinary_purge_export(
                records_bytes, manifest_bytes
            )
        except QueueOperationError:
            raise
        except (OSError, PermissionError, UnicodeError, ValueError) as exc:
            raise QueueOperationError("export_verification_failed") from exc
        task_ids = tuple(str(record["task_id"]) for record in records)
        self._require_ordinary_purge_manifest(
            manifest,
            cutoff=cutoff,
            task_ids=task_ids,
            records_bytes=records_bytes,
        )
        capture_evidence = self._capture_purge_evidence_from_manifest(
            manifest["capture_artifacts"], records
        )
        plan = _OrdinaryPurgePlan(
            cutoff,
            export,
            task_ids,
            records,
            records_bytes,
            capture_evidence,
            manifest_bytes,
        )
        self._require_existing_ordinary_purge_files(plan, manifest)
        return plan

    @staticmethod
    def _require_ordinary_purge_export_directory(export: Path) -> None:
        metadata = export.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        actual = (
            stat.S_ISDIR(metadata.st_mode),
            stat.S_ISLNK(metadata.st_mode),
            bool(getattr(metadata, "st_file_attributes", 0) & reparse),
            _is_owner_only(export),
        )
        if actual != (True, False, False, True):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _decode_ordinary_purge_export(
        records_bytes: bytes, manifest_bytes: bytes
    ) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
        records_value, manifest = (
            _QueueV3CandidateReader._decode_ordinary_purge_json(
                records_bytes, manifest_bytes
            )
        )
        records = _QueueV3CandidateReader._ordinary_purge_records(
            records_value, records_bytes
        )
        return records, manifest

    @staticmethod
    def _decode_ordinary_purge_json(
        records_bytes: bytes, manifest_bytes: bytes
    ) -> tuple[list[object], dict[str, object]]:
        try:
            records_value = json.loads(records_bytes.decode("utf-8", errors="strict"))
            manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise QueueOperationError("export_verification_failed") from exc
        if (isinstance(records_value, list), isinstance(manifest, dict)) != (True, True):
            raise QueueOperationError("export_verification_failed")
        return records_value, manifest

    @staticmethod
    def _ordinary_purge_records(
        records_value: list[object], records_bytes: bytes
    ) -> tuple[dict[str, object], ...]:
        if not all(isinstance(record, dict) for record in records_value):
            raise QueueOperationError("export_verification_failed")
        records = tuple(records_value)
        if canonical_json_bytes(list(records)) != records_bytes:
            raise QueueOperationError("export_verification_failed")
        for record in records:
            validate_schema(
                record, Path(__file__).with_name("schemas") / "queue-task-v3.json"
            )
        return records

    @staticmethod
    def _require_ordinary_purge_manifest(
        manifest: Mapping[str, object],
        *,
        cutoff: str,
        task_ids: tuple[str, ...],
        records_bytes: bytes,
    ) -> None:
        identity = (
            set(manifest),
            manifest.get("cutoff"),
            manifest.get("records_sha256"),
            manifest.get("task_ids"),
        )
        expected = (
            {"capture_artifacts", "cutoff", "records_sha256", "results", "task_ids"},
            cutoff,
            sha256_bytes(records_bytes),
            list(task_ids),
        )
        if identity != expected:
            raise QueueOperationError("export_verification_failed")
        if len(set(task_ids)) != len(task_ids):
            raise QueueOperationError("export_verification_failed")
        if not isinstance(manifest.get("results"), list):
            raise QueueOperationError("export_verification_failed")
        if not isinstance(manifest.get("capture_artifacts"), list):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _capture_purge_manifest_identity(
        value: object,
    ) -> tuple[str, str, str, str, str, str, str]:
        record = _QueueV3CandidateReader._capture_purge_manifest_mapping(value)
        _QueueV3CandidateReader._require_capture_purge_manifest_hashes(record)
        return (
            str(record["task_id"]),
            str(record["intent_id"]),
            str(record["terminal_path"]),
            str(record["kind"]),
            str(record["source_path"]),
            str(record["archive_path"]),
            str(record["sha256"]),
        )

    @staticmethod
    def _capture_purge_manifest_mapping(value: object) -> dict[str, object]:
        fields = {
            "archive_path",
            "intent_id",
            "kind",
            "sha256",
            "source_path",
            "task_id",
            "terminal_path",
        }
        if not isinstance(value, dict):
            raise QueueOperationError("export_verification_failed")
        actual = (set(value), {type(item) for item in value.values()})
        if actual != (fields, {str}):
            raise QueueOperationError("export_verification_failed")
        return value

    @staticmethod
    def _require_capture_purge_manifest_hashes(
        value: Mapping[str, object],
    ) -> None:
        if value["kind"] not in {"intent", "decision"}:
            raise QueueOperationError("export_verification_failed")
        try:
            _require_lower_sha256(str(value["intent_id"]), "intent_id")
            _require_lower_sha256(str(value["sha256"]), "sha256")
        except ValueError as exc:
            raise QueueOperationError("export_verification_failed") from exc

    @staticmethod
    def _capture_purge_manifest_artifact(
        task_id: str,
        source_path: str,
        archive_path: str,
        digest: str,
    ) -> _CapturePurgeArtifact:
        try:
            restricted_relative_path(
                source_path, ("run/capture-intents", "run/queue-results")
            )
            restricted_relative_path(archive_path, ("capture-artifacts",))
        except ValueError as exc:
            raise QueueOperationError("export_verification_failed") from exc
        if archive_path != _capture_purge_archive_path(task_id, source_path):
            raise QueueOperationError("export_verification_failed")
        return _CapturePurgeArtifact(source_path, archive_path, digest)

    def _capture_purge_evidence_from_manifest(
        self,
        value: object,
        records: tuple[dict[str, object], ...],
    ) -> tuple[_CapturePurgeEvidence, ...]:
        if not isinstance(value, list):
            raise QueueOperationError("export_verification_failed")
        grouped: dict[
            tuple[str, str, str], list[tuple[str, _CapturePurgeArtifact]]
        ] = {}
        for item in value:
            task_id, intent_id, terminal, kind, source, archive, digest = (
                self._capture_purge_manifest_identity(item)
            )
            artifact = self._capture_purge_manifest_artifact(
                task_id, source, archive, digest
            )
            grouped.setdefault((task_id, intent_id, terminal), []).append(
                (kind, artifact)
            )
        evidence = tuple(
            self._capture_purge_evidence_group(key, grouped[key])
            for key in sorted(grouped)
        )
        self._require_capture_purge_evidence_records(evidence, records)
        return evidence

    @staticmethod
    def _capture_purge_evidence_group(
        key: tuple[str, str, str],
        items: list[tuple[str, _CapturePurgeArtifact]],
    ) -> _CapturePurgeEvidence:
        intents, decisions = _QueueV3CandidateReader._partition_capture_purge_artifacts(
            items
        )
        _QueueV3CandidateReader._require_unique_capture_purge_paths(items, intents)
        task_id, intent_id, terminal_path = key
        return _CapturePurgeEvidence(
            task_id, intent_id, intents[0], decisions, terminal_path
        )

    @staticmethod
    def _partition_capture_purge_artifacts(
        items: list[tuple[str, _CapturePurgeArtifact]],
    ) -> tuple[tuple[_CapturePurgeArtifact, ...], tuple[_CapturePurgeArtifact, ...]]:
        intents: list[_CapturePurgeArtifact] = []
        decisions: list[_CapturePurgeArtifact] = []
        for kind, artifact in items:
            if kind == "intent":
                intents.append(artifact)
                continue
            decisions.append(artifact)
        return tuple(intents), tuple(decisions)

    @staticmethod
    def _require_unique_capture_purge_paths(
        items: list[tuple[str, _CapturePurgeArtifact]],
        intents: tuple[_CapturePurgeArtifact, ...],
    ) -> None:
        paths = {artifact.source_path for _kind, artifact in items}
        if (len(intents), len(paths)) != (1, len(items)):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _ordinary_purge_record_capture_identity(
        record: Mapping[str, object],
    ) -> tuple[str, str, str, str] | None:
        binding = record.get("capture_binding")
        if binding is None:
            return None
        result = record.get("result")
        if (isinstance(binding, dict), isinstance(result, dict)) != (True, True):
            raise QueueOperationError("export_verification_failed")
        return _QueueV3CandidateReader._ordinary_capture_record_values(
            record, binding, result
        )

    @staticmethod
    def _ordinary_capture_record_values(
        record: Mapping[str, object],
        binding: Mapping[str, object],
        result: Mapping[str, object],
    ) -> tuple[str, str, str, str]:
        task_id = record.get("task_id")
        intent_id = binding.get("intent_id")
        intent_sha256 = binding.get("intent_sha256")
        terminal_path = result.get("reference")
        values = (task_id, intent_id, intent_sha256, terminal_path)
        if tuple(isinstance(item, str) for item in values) != (True,) * len(values):
            raise QueueOperationError("export_verification_failed")
        if terminal_path != f"run/queue-results/capture-{intent_id}.json":
            raise QueueOperationError("export_verification_failed")
        return str(task_id), str(intent_id), str(intent_sha256), str(terminal_path)

    def _require_capture_purge_evidence_records(
        self,
        evidence: tuple[_CapturePurgeEvidence, ...],
        records: tuple[dict[str, object], ...],
    ) -> None:
        expected = self._ordinary_purge_expected_capture_records(records)
        observed = {
            item.task_id: (
                item.intent_id,
                item.intent.sha256,
                item.terminal_path,
            )
            for item in evidence
        }
        if (len(observed), observed) != (len(evidence), expected):
            raise QueueOperationError("export_verification_failed")

    def _ordinary_purge_expected_capture_records(
        self, records: tuple[dict[str, object], ...]
    ) -> dict[str, tuple[str, str, str]]:
        expected: dict[str, tuple[str, str, str]] = {}
        for record in records:
            identity = self._ordinary_purge_record_capture_identity(record)
            if identity is not None:
                task_id, intent_id, intent_sha256, terminal = identity
                expected[task_id] = (intent_id, intent_sha256, terminal)
        return expected

    @staticmethod
    def _ordinary_purge_expected_results(
        records: tuple[dict[str, object], ...],
    ) -> list[dict[str, str]]:
        return [
            {"id": str(record["task_id"]), "sha256": str(record["result"]["sha256"])}
            for record in records
            if isinstance(record.get("result"), dict)
        ]

    @staticmethod
    def _capture_purge_artifacts(
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> tuple[_CapturePurgeArtifact, ...]:
        return tuple(
            artifact
            for item in evidence
            for artifact in (item.intent, *item.decisions)
        )

    def _require_existing_ordinary_purge_files(
        self, plan: _OrdinaryPurgePlan, manifest: Mapping[str, object]
    ) -> None:
        expected_results = self._ordinary_purge_expected_results(plan.records)
        if manifest["results"] != expected_results:
            raise QueueOperationError("export_verification_failed")
        self._require_existing_ordinary_result_files(plan.export, expected_results)
        self._require_existing_capture_purge_files(plan)

    @staticmethod
    def _require_existing_ordinary_result_files(
        export: Path, expected_results: list[dict[str, str]]
    ) -> None:
        for item in expected_results:
            relative = _ordinary_purge_result_archive_path(item["id"])
            data = _read_stable_owner_file(export / relative, _MAX_RESULT_BYTES)
            if sha256_bytes(data) != item["sha256"]:
                raise QueueOperationError("export_verification_failed")

    def _require_existing_capture_purge_files(
        self, plan: _OrdinaryPurgePlan
    ) -> None:
        for artifact in self._capture_purge_artifacts(plan.capture_evidence):
            data = _read_stable_owner_file(
                plan.export / artifact.archive_path, _MAX_QUEUE_PAYLOAD_BYTES
            )
            if sha256_bytes(data) != artifact.sha256:
                raise QueueOperationError("export_verification_failed")

    def _publish_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        if plan.manifest_bytes is not None:
            return self._existing_ordinary_purge_manifest(plan)
        return self._publish_new_ordinary_purge_export(
            plan, deadline=deadline, cancelled=cancelled
        )

    @staticmethod
    def _existing_ordinary_purge_manifest(plan: _OrdinaryPurgePlan) -> bytes:
        current = _read_stable_owner_file(
            plan.export / "manifest.json", _MAX_EXPORT_METADATA_BYTES
        )
        if current != plan.manifest_bytes:
            raise QueueOperationError("export_verification_failed")
        return current

    def _publish_new_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        parent = plan.export.parent
        parent.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(parent, 0o700)
        actual = (parent.is_symlink(), parent.is_dir(), _is_owner_only(parent))
        if actual != (False, True, True):
            raise QueueOperationError("export_parent_permissions_invalid")
        _cleanup_export_staging(parent, plan.export.name)
        staging = parent / f".{plan.export.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        _harden_owner_only(staging, 0o700)
        try:
            manifest_bytes = self._write_ordinary_purge_export(
                plan, staging, deadline=deadline, cancelled=cancelled
            )
            _require_active(deadline, cancelled)
            staging.replace(plan.export)
            fsync_directory(parent)
            return manifest_bytes
        except BaseException:
            _remove_export_staging(staging)
            raise

    def _write_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        staging: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        results_export = staging / "results"
        results_export.mkdir()
        _harden_owner_only(results_export, 0o700)
        results = self._ordinary_purge_result_manifest(
            plan.records,
            results_export,
            deadline=deadline,
            cancelled=cancelled,
        )
        capture_artifacts = self._ordinary_purge_capture_manifest(
            plan,
            staging,
            deadline=deadline,
            cancelled=cancelled,
        )
        manifest_bytes = canonical_json_bytes(
            {
                "capture_artifacts": capture_artifacts,
                "cutoff": plan.cutoff,
                "records_sha256": sha256_bytes(plan.records_bytes),
                "results": results,
                "task_ids": list(plan.task_ids),
            }
        )
        records_path = staging / "records.json"
        manifest_path = staging / "manifest.json"
        _write_durable_file(records_path, plan.records_bytes)
        _write_durable_file(manifest_path, manifest_bytes)
        self._require_ordinary_purge_export(
            records_path, plan.records_bytes, manifest_path, manifest_bytes
        )
        fsync_directory(results_export)
        fsync_directory(staging)
        return manifest_bytes

    def _ordinary_purge_capture_manifest(
        self,
        plan: _OrdinaryPurgePlan,
        staging: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        manifest: list[dict[str, str]] = []
        for evidence in plan.capture_evidence:
            _require_active(deadline, cancelled)
            manifest.append(
                self._publish_capture_purge_artifact(
                    staging, evidence, evidence.intent, kind="intent"
                )
            )
            for decision in evidence.decisions:
                manifest.append(
                    self._publish_capture_purge_artifact(
                        staging, evidence, decision, kind="decision"
                    )
                )
        capture_dir = staging / "capture-artifacts"
        if capture_dir.exists():
            fsync_directory(capture_dir)
        return manifest

    def _publish_capture_purge_artifact(
        self,
        staging: Path,
        evidence: _CapturePurgeEvidence,
        artifact: _CapturePurgeArtifact,
        *,
        kind: Literal["intent", "decision"],
    ) -> dict[str, str]:
        data = read_runtime_bytes(
            self.state_root / artifact.source_path,
            self.state_root,
            max_bytes=_MAX_QUEUE_PAYLOAD_BYTES,
            owner_only=True,
        )
        if sha256_bytes(data) != artifact.sha256:
            raise QueueOperationError("capture_purge_changed")
        target = staging / artifact.archive_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(target.parent, 0o700)
        _write_durable_file(target, data)
        return {
            "archive_path": artifact.archive_path,
            "intent_id": evidence.intent_id,
            "kind": kind,
            "sha256": artifact.sha256,
            "source_path": artifact.source_path,
            "task_id": evidence.task_id,
            "terminal_path": evidence.terminal_path,
        }

    def _ordinary_purge_result_manifest(
        self,
        records: tuple[dict[str, object], ...],
        results_export: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        result_manifest: list[dict[str, str]] = []
        for record in records:
            _require_active(deadline, cancelled)
            result = record["result"]
            if not isinstance(result, dict):
                continue
            reference = str(result["reference"])
            digest = str(result["sha256"])
            data = _read_stable_owner_file(
                self.state_root / reference, _MAX_RESULT_BYTES
            )
            if sha256_bytes(data) != digest:
                raise QueueOperationError("result_verification_failed")
            archive = _ordinary_purge_result_archive_path(str(record["task_id"]))
            _write_durable_file(results_export.parent / archive, data)
            result_manifest.append({"id": str(record["task_id"]), "sha256": digest})
        return result_manifest

    @staticmethod
    def _require_ordinary_purge_export(
        records_path: Path,
        records_bytes: bytes,
        manifest_path: Path,
        manifest_bytes: bytes,
    ) -> None:
        actual = (
            _read_stable_owner_file(records_path, _MAX_EXPORT_METADATA_BYTES),
            _read_stable_owner_file(manifest_path, _MAX_EXPORT_METADATA_BYTES),
        )
        if actual != (records_bytes, manifest_bytes):
            raise QueueOperationError("export_verification_failed")

    def _commit_ordinary_purge(
        self,
        plan: _OrdinaryPurgePlan,
        manifest_bytes: bytes,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[_CapturePurgeEvidence, ...]:
        if not plan.task_ids:
            return ()
        manifest_sha256 = sha256_bytes(manifest_bytes)
        operation_id = _ordinary_purge_operation_id(manifest_sha256)
        receipt_published = self._ordinary_purge_receipt_matches(
            plan, manifest_bytes
        )
        expected = {item.task_id: item for item in plan.capture_evidence}
        with closing(self._connect()) as database, begin_immediate(
            database,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            state = self._ordinary_purge_commit_state(
                database,
                plan,
                operation_id=operation_id,
                manifest_sha256=manifest_sha256,
                receipt_published=receipt_published,
            )
            if state == "pending":
                for task_id, expected_record in zip(plan.task_ids, plan.records):
                    _require_active(deadline, cancelled)
                    self._purge_ordinary_task(
                        database,
                        task_id,
                        expected_record,
                        expected_capture=expected.get(task_id),
                        cutoff=plan.cutoff,
                        operation_id=operation_id,
                        manifest_sha256=manifest_sha256,
                    )
        return plan.capture_evidence

    def _ordinary_purge_commit_state(
        self,
        database: sqlite3.Connection,
        plan: _OrdinaryPurgePlan,
        *,
        operation_id: str,
        manifest_sha256: str,
        receipt_published: bool,
    ) -> str:
        placeholders = ",".join("?" for _ in plan.task_ids)
        present = database.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
            plan.task_ids,
        ).fetchall()
        authorizations = database.execute(
            f"""SELECT * FROM task_purge_authorizations
                WHERE task_id IN ({placeholders}) ORDER BY task_id""",  # noqa: S608
            plan.task_ids,
        ).fetchall()
        counts = (len(present), len(authorizations))
        if counts == (len(plan.task_ids), 0):
            return "pending"
        if counts == (0, len(plan.task_ids)):
            self._require_ordinary_purge_authorizations(
                authorizations, plan.task_ids, operation_id, manifest_sha256
            )
            return "cleanup"
        if (counts, receipt_published) == ((0, 0), True):
            return "complete"
        raise QueueOperationError("purge_resume_conflict")

    @staticmethod
    def _require_ordinary_purge_authorizations(
        rows: list[sqlite3.Row],
        task_ids: tuple[str, ...],
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        expected = sorted(
            (
                task_id,
                "ordinary",
                operation_id,
                _ordinary_purge_authorization_digest(
                    task_id, operation_id, manifest_sha256
                ),
            )
            for task_id in task_ids
        )
        actual = sorted(
            (
                str(row["task_id"]),
                str(row["mode"]),
                str(row["operation_id"]),
                str(row["authorization_digest"]),
            )
            for row in rows
        )
        if actual != expected:
            raise QueueOperationError("purge_authorization_failed")

    def _purge_ordinary_task(
        self,
        database: sqlite3.Connection,
        task_id: str,
        expected_record: Mapping[str, object],
        *,
        expected_capture: _CapturePurgeEvidence | None,
        cutoff: str,
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        row = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        self._require_ordinary_purge_task(row, cutoff)
        self._require_ordinary_purge_payload(database, row)
        self._require_ordinary_purge_record(database, row, expected_record)
        evidence = self._ordinary_capture_purge_evidence(database, task_id)
        expected_evidence = () if expected_capture is None else (expected_capture,)
        if evidence != expected_evidence:
            raise QueueOperationError("capture_purge_changed")
        self._insert_ordinary_purge_authorization(
            database, task_id, operation_id, manifest_sha256
        )
        self._delete_capture_purge_rows_before_task(database, evidence)
        self._delete_task_owned_evidence(database, task_id)
        self._delete_ordinary_task(database, task_id)
        self._delete_capture_purge_rows_after_task(database, evidence)

    @staticmethod
    def _require_ordinary_purge_task(row: sqlite3.Row | None, cutoff: str) -> None:
        if (
            row is None
            or row["state"] not in {"succeeded", "cancelled"}
            or row["updated_at"] >= cutoff
        ):
            raise QueueOperationError("purge_selection_changed")

    def _require_ordinary_purge_payload(
        self, database: sqlite3.Connection, row: sqlite3.Row
    ) -> None:
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=True
        )
        if validation.code is not None:
            self._demote_payload_mismatch(database, row, now=_utc_now())
            raise QueueOperationError("payload_hash_mismatch")

    def _require_ordinary_purge_record(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        expected_record: Mapping[str, object],
    ) -> None:
        current = self._export_task_in_transaction(database, row)
        if canonical_json_bytes(current) != canonical_json_bytes(expected_record):
            raise QueueOperationError("purge_selection_changed")

    def _ordinary_capture_purge_evidence(
        self, database: sqlite3.Connection, task_id: str
    ) -> tuple[_CapturePurgeEvidence, ...]:
        link = database.execute(
            "SELECT 1 FROM capture_task_links WHERE task_id=?", (task_id,)
        ).fetchone()
        if link is None:
            return ()
        binding = self.active_capture_binding(database, task_id)
        return (self._capture_purge_evidence(database, task_id, binding),)

    @staticmethod
    def _insert_ordinary_purge_authorization(
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        authorization_digest = _ordinary_purge_authorization_digest(
            task_id, operation_id, manifest_sha256
        )
        inserted = database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'ordinary',?,?,?)""",
            (task_id, operation_id, authorization_digest, _timestamp(_utc_now())),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("purge_authorization_failed")

    @staticmethod
    def _delete_capture_purge_rows_before_task(
        database: sqlite3.Connection,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        for item in evidence:
            deleted = database.execute(
                "DELETE FROM semantic_decisions WHERE intent_id=?", (item.intent_id,)
            ).rowcount
            if deleted != len(item.decisions):
                raise QueueOperationError("capture_purge_changed")

    @staticmethod
    def _delete_ordinary_task(
        database: sqlite3.Connection, task_id: str
    ) -> None:
        deleted = database.execute(
            "DELETE FROM tasks WHERE id=?", (task_id,)
        ).rowcount
        if deleted != 1:
            raise QueueOperationError("purge_delete_failed")

    @staticmethod
    def _delete_capture_purge_rows_after_task(
        database: sqlite3.Connection,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        for item in evidence:
            deleted = database.execute(
                "DELETE FROM capture_intents WHERE intent_id=?", (item.intent_id,)
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("capture_purge_changed")

    def _cleanup_ordinary_purge_artifacts(
        self,
        plan: _OrdinaryPurgePlan,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        retained_terminals = {item.terminal_path for item in evidence}
        for item in evidence:
            self._unlink_capture_purge_artifacts(plan.export, item)
        for record in plan.records:
            self._cleanup_ordinary_result(record, retained_terminals)

    def _cleanup_ordinary_result(
        self, record: Mapping[str, object], retained_terminals: set[str]
    ) -> None:
        result = record["result"]
        if not isinstance(result, dict):
            return
        reference = str(result["reference"])
        if reference in retained_terminals:
            return
        with closing(self._connect()) as database:
            retained = database.execute(
                "SELECT 1 FROM tasks WHERE result_reference=? LIMIT 1", (reference,)
            ).fetchone()
        if retained is None:
            (self.state_root / reference).unlink(missing_ok=True)

    def _unlink_capture_purge_artifacts(
        self, export: Path, evidence: _CapturePurgeEvidence
    ) -> None:
        for artifact in (evidence.intent, *evidence.decisions):
            self._unlink_capture_purge_artifact(export, evidence.task_id, artifact)

    def _unlink_capture_purge_artifact(
        self,
        export: Path,
        task_id: str,
        artifact: _CapturePurgeArtifact,
    ) -> None:
        self._capture_purge_manifest_artifact(
            task_id, artifact.source_path, artifact.archive_path, artifact.sha256
        )
        archived = _read_stable_owner_file(
            export / artifact.archive_path, _MAX_QUEUE_PAYLOAD_BYTES
        )
        if sha256_bytes(archived) != artifact.sha256:
            raise QueueOperationError("export_verification_failed")
        source = self.state_root / artifact.source_path
        try:
            before = source.lstat()
        except FileNotFoundError:
            return
        current = read_runtime_bytes(
            source,
            self.state_root,
            max_bytes=_MAX_QUEUE_PAYLOAD_BYTES,
            owner_only=True,
        )
        if sha256_bytes(current) != artifact.sha256:
            raise QueueOperationError("capture_purge_changed")
        if not os.path.samestat(before, source.lstat()):
            raise QueueOperationError("capture_purge_changed")
        source.unlink()
        fsync_directory(source.parent)

    @staticmethod
    def _ordinary_purge_receipt_bytes(
        plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> bytes:
        manifest_sha256 = sha256_bytes(manifest_bytes)
        return canonical_json_bytes(
            {
                "schema_version": "ordinary-purge-receipt/v1",
                "operation_id": _ordinary_purge_operation_id(manifest_sha256),
                "manifest_sha256": manifest_sha256,
                "task_ids": list(plan.task_ids),
            }
        )

    def _ordinary_purge_receipt_matches(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> bool:
        path = plan.export / "purge-receipt.json"
        try:
            current = _read_stable_owner_file(path, 64 * 1024)
        except FileNotFoundError:
            return False
        if current != self._ordinary_purge_receipt_bytes(plan, manifest_bytes):
            raise QueueOperationError("purge_receipt_invalid")
        return True

    def _publish_ordinary_purge_receipt(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> None:
        receipt_bytes = self._ordinary_purge_receipt_bytes(plan, manifest_bytes)
        _write_durable_file(plan.export / "purge-receipt.json", receipt_bytes)
        if not self._ordinary_purge_receipt_matches(plan, manifest_bytes):
            raise QueueOperationError("purge_receipt_invalid")

    def _clear_ordinary_purge_authorizations(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> None:
        if not plan.task_ids:
            return
        manifest_sha256 = sha256_bytes(manifest_bytes)
        operation_id = _ordinary_purge_operation_id(manifest_sha256)
        placeholders = ",".join("?" for _ in plan.task_ids)
        with closing(self._connect()) as database, begin_immediate(database):
            rows = database.execute(
                f"""SELECT * FROM task_purge_authorizations
                    WHERE task_id IN ({placeholders}) ORDER BY task_id""",  # noqa: S608
                plan.task_ids,
            ).fetchall()
            if not rows:
                return
            self._require_ordinary_purge_authorizations(
                rows, plan.task_ids, operation_id, manifest_sha256
            )
            deleted = database.execute(
                f"""DELETE FROM task_purge_authorizations
                    WHERE task_id IN ({placeholders})""",  # noqa: S608
                plan.task_ids,
            ).rowcount
            if deleted != len(plan.task_ids):
                raise QueueOperationError("purge_authorization_failed")

    def _export_task_in_transaction(
        self, database: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, object]:
        task_id = str(row["id"])
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=True
        )
        if validation.code is not None:
            raise QueueOperationError("payload_hash_mismatch")
        history = database.execute(
            """SELECT attempt,started_at,finished_at,outcome,error_code
               FROM attempt_history WHERE task_id=? ORDER BY sequence""",
            (task_id,),
        ).fetchall()
        sources = database.execute(
            """SELECT logical_path,source_digest FROM task_source_links
               WHERE task_id=? ORDER BY logical_path,source_digest""",
            (task_id,),
        ).fetchall()
        link = database.execute(
            "SELECT * FROM capture_task_links WHERE task_id=?", (task_id,)
        ).fetchone()
        seal = database.execute(
            "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
        ).fetchone()
        lease = None
        if row["lease_owner"] is not None:
            lease = {
                "owner": row["lease_owner"],
                "token": row["lease_token"],
                "expires_at": row["lease_expires_at"],
                "heartbeat_at": row["lease_heartbeat_at"],
            }
        result = None
        if row["result_reference"] is not None:
            result = {
                "reference": row["result_reference"],
                "sha256": row["result_sha256"],
                "operation_id": row["result_operation_id"],
            }
        capture_binding = None
        if link is not None:
            capture_binding = {
                "intent_id": link["intent_id"],
                "intent_sha256": link["intent_sha256"],
                "handler_version": int(link["handler_version"]),
                "active_digest": str(link["link_digest"]),
                "seal_digest": None if seal is None else seal["seal_digest"],
            }
        record: dict[str, object] = {
            "schema_version": "queue-task/v3",
            "task_id": task_id,
            "kind": str(row["kind"]),
            "handler_version": int(row["handler_version"]),
            "payload": validation.payload,
            "input_hash": str(row["input_hash"]),
            "dedupe_key": row["dedupe_key"],
            "state": str(row["state"]),
            "priority": int(row["priority"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "available_at": str(row["available_at"]),
            "attempts": int(row["attempts"]),
            "last_attempt_at": row["last_attempt_at"],
            "lease": lease,
            "error_code": row["error_code"],
            "blocked_capability": row["blocked_capability"],
            "result": result,
            "redrive_of": row["redrive_of"],
            "lineage_generation": int(row["lineage_generation"]),
            "attempt_history": [
                {
                    "attempt": int(item["attempt"]),
                    "started_at": str(item["started_at"]),
                    "finished_at": str(item["finished_at"]),
                    "outcome": str(item["outcome"]),
                    "error_code": item["error_code"],
                }
                for item in history
            ],
            "source_links": [
                {
                    "logical_path": str(item["logical_path"]),
                    "source_digest": str(item["source_digest"]),
                }
                for item in sources
            ],
            "capture_binding": capture_binding,
        }
        validate_schema(
            record, Path(__file__).with_name("schemas") / "queue-task-v3.json"
        )
        return record


@contextmanager
def capture_task_fences(
    queue: _QueueV3CandidateReader,
    coordinator: object,
    task_id: str,
    *,
    intent_id: str | None,
    mode: Literal["worker", "queue-operator"],
    owner: OwnerLease,
) -> Iterator[tuple[TaskFence, object | None]]:
    task_fence = queue.acquire_task_fence(task_id, mode=mode, owner=owner)
    intent_fence = None
    try:
        if intent_id is not None:
            intent_mode = "worker" if mode == "worker" else "operator"
            intent_fence = coordinator.acquire_intent_fence(
                intent_id, mode=intent_mode, owner=owner
            )
        yield task_fence, intent_fence
    finally:
        if intent_fence is not None:
            coordinator.release_intent_fence(intent_fence)
        queue.release_task_fence(task_fence)


def _state_root() -> Path:
    env = os.environ.get("LLM_WIKI_STATE_ROOT")
    if env:
        return Path(env)
    try:
        from memory_state import STATE_ROOT

        return Path(STATE_ROOT)
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent))


def _pid_is_alive(pid: int) -> bool:
    try:
        from memory_state import _is_pid_alive

        return _is_pid_alive(pid)
    except Exception:  # noqa: BLE001 - inability to prove death is treated as live
        return True


def _migration_paths(state_root: Path) -> tuple[Path, Path, Path, Path]:
    run_dir = Path(state_root).resolve() / "run"
    return (
        run_dir,
        run_dir / "queue",
        run_dir / "queue.sqlite3",
        run_dir / "queue-migrated-v2",
    )


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _legacy_write_allowed(state_root: Path) -> bool:
    _run_dir, legacy_dir, _db_path, marker = _migration_paths(state_root)
    if _path_present(marker):
        _validate_migration_marker(marker)
        if legacy_dir.exists():
            _post_marker_legacy_conflict(state_root)
        raise LegacyBackendDisabled("legacy_backend_disabled")
    if _queue_owner_is_active(state_root, "migration"):
        raise LegacyBackendDisabled("legacy_migration_quiesced")
    return True


def _open_queue_ownership_db(state_root: Path) -> sqlite3.Connection:
    run_dir, _legacy_dir, db_path, _marker = _migration_paths(state_root)
    run_dir.mkdir(parents=True, exist_ok=True)
    _harden_owner_only(run_dir, 0o700)
    connection = open_operational_db(db_path, busy_ms=DEFAULTS.queue_busy_ms)
    try:
        _harden_owner_only(db_path, 0o600)
        connection.execute(
            """CREATE TABLE IF NOT EXISTS queue_ownership (
                   role TEXT PRIMARY KEY,
                   token TEXT,
                   pid INTEGER,
                   heartbeat_at TEXT,
                   expires_at TEXT,
                   epoch INTEGER NOT NULL CHECK (epoch >= 0)
               )"""
        )
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection


def _acquire_queue_owner(
    state_root: Path,
    role: str,
    busy_code: str,
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULTS.queue_lease_seconds,
) -> QueueOwnerLease:
    if not role or ttl_seconds <= 0:
        raise ValueError("owner role and ttl must be valid")
    acquired_at = _as_utc(now or _utc_now())
    token = uuid.uuid4().hex
    pid = os.getpid()
    expires_at = acquired_at + timedelta(seconds=ttl_seconds)
    with _open_queue_ownership_db(state_root) as connection, begin_immediate(connection):
        row = connection.execute(
            "SELECT * FROM queue_ownership WHERE role=?", (role,)
        ).fetchone()
        epoch = 1
        if row is not None:
            epoch = int(row["epoch"]) + 1
            existing_token = row["token"]
            if existing_token is not None:
                existing_pid = row["pid"]
                dead = not isinstance(existing_pid, int) or not _pid_is_alive(existing_pid)
                if not dead:
                    raise MigrationBusy(busy_code)
            connection.execute(
                """UPDATE queue_ownership
                   SET token=?, pid=?, heartbeat_at=?, expires_at=?, epoch=?
                   WHERE role=?""",
                (
                    token,
                    pid,
                    _timestamp(acquired_at),
                    _timestamp(expires_at),
                    epoch,
                    role,
                ),
            )
        else:
            connection.execute(
                """INSERT INTO queue_ownership(
                       role, token, pid, heartbeat_at, expires_at, epoch
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    role,
                    token,
                    pid,
                    _timestamp(acquired_at),
                    _timestamp(expires_at),
                    epoch,
                ),
            )
    return QueueOwnerLease(
        Path(state_root).resolve(), role, token, pid, epoch, expires_at, ttl_seconds
    )


def _require_queue_owner(
    connection: sqlite3.Connection,
    lease: QueueOwnerLease,
    now: datetime,
    *,
    heartbeat: bool,
) -> datetime:
    row = connection.execute(
        "SELECT * FROM queue_ownership WHERE role=?", (lease.role,)
    ).fetchone()
    expiry = _parse_timestamp(row["expires_at"]) if row is not None else None
    if (
        row is None
        or row["token"] != lease.token
        or int(row["epoch"]) != lease.epoch
        or expiry is None
        or row["pid"] != lease.pid
        or not _pid_is_alive(lease.pid)
    ):
        code = "migration_fence_lost" if lease.role == "migration" else "legacy_owner_fence_lost"
        raise QueueOperationError(code)
    if heartbeat:
        expiry = now + timedelta(seconds=lease.ttl_seconds)
        connection.execute(
            """UPDATE queue_ownership SET heartbeat_at=?, expires_at=?
               WHERE role=? AND token=? AND epoch=?""",
            (
                _timestamp(now),
                _timestamp(expiry),
                lease.role,
                lease.token,
                lease.epoch,
            ),
        )
    return expiry


def _heartbeat_queue_owner(
    lease: QueueOwnerLease, *, now: datetime | None = None
) -> QueueOwnerLease:
    heartbeat_at = _as_utc(now or _utc_now())
    with _open_queue_ownership_db(lease.state_root) as connection, begin_immediate(connection):
        expires_at = _require_queue_owner(
            connection, lease, heartbeat_at, heartbeat=True
        )
    return replace(lease, expires_at=expires_at)


def _release_queue_owner(lease: QueueOwnerLease) -> bool:
    with _open_queue_ownership_db(lease.state_root) as connection, begin_immediate(connection):
        changed = connection.execute(
            """UPDATE queue_ownership
               SET token=NULL, pid=NULL, heartbeat_at=NULL, expires_at=NULL
               WHERE role=? AND token=? AND epoch=?""",
            (lease.role, lease.token, lease.epoch),
        ).rowcount
    return changed == 1


def _queue_owner_is_active(state_root: Path, role: str) -> bool:
    with _open_queue_ownership_db(state_root) as connection:
        row = connection.execute(
            "SELECT token, pid, expires_at FROM queue_ownership WHERE role=?", (role,)
        ).fetchone()
    if row is None or row["token"] is None:
        return False
    return (
        isinstance(row["pid"], int)
        and _pid_is_alive(row["pid"])
    )


def _validate_migration_marker(marker: Path) -> None:
    expected = canonical_json_bytes({"version": 2})
    try:
        metadata = marker.lstat()
        if (
            marker.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > _MAX_MARKER_BYTES
            or not _is_owner_only(marker)
        ):
            raise QueueOperationError("migration_marker_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(marker, flags)
        try:
            opened = os.fstat(descriptor)
            if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
                raise QueueOperationError("migration_marker_invalid")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(_MAX_MARKER_BYTES + 1)
            after = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise QueueOperationError("migration_marker_invalid")
        finally:
            os.close(descriptor)
        if raw != expected:
            raise QueueOperationError("migration_marker_invalid")
    except QueueOperationError:
        raise
    except (OSError, ValueError):
        raise QueueOperationError("migration_marker_invalid") from None


def _read_bounded_regular_nofollow(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise QueueOperationError("legacy_source_unsafe")
    if metadata.st_size > _MAX_LEGACY_RECORD_BYTES:
        raise QueueOperationError("legacy_record_too_large")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_LEGACY_RECORD_BYTES:
            raise QueueOperationError("legacy_source_unsafe")
        if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            raise QueueOperationError("legacy_source_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(_MAX_LEGACY_RECORD_BYTES + 1)
        if len(raw) > _MAX_LEGACY_RECORD_BYTES:
            raise QueueOperationError("legacy_record_too_large")
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise QueueOperationError("legacy_source_changed")
        return raw
    finally:
        os.close(descriptor)


def _prove_no_live_processing(legacy_dir: Path) -> None:
    if not legacy_dir.exists():
        return
    for path in legacy_dir.glob("*.processing"):
        raw = _read_bounded_regular_nofollow(path)
        try:
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MigrationBusy("legacy_owner_unverifiable") from None
        pid = record.get("lease_pid") if isinstance(record, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            raise MigrationBusy("legacy_owner_unverifiable")
        if _pid_is_alive(pid):
            raise MigrationBusy("legacy_owner_live")


def _scan_legacy_records(
    legacy_dir: Path,
) -> tuple[list[tuple[Path, dict[str, object]]], list[tuple[Path, bytes]]]:
    valid: list[tuple[Path, dict[str, object]]] = []
    malformed: list[tuple[Path, bytes]] = []
    if not legacy_dir.exists():
        return valid, malformed
    paths = sorted((*legacy_dir.glob("*.json"), *legacy_dir.glob("*.processing")))
    for path in paths:
        try:
            raw = _read_bounded_regular_nofollow(path)
            record = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            malformed.append((path, raw))
            continue
        if path.suffix == ".processing":
            pid = record.get("lease_pid") if isinstance(record, dict) else None
            if isinstance(pid, int) and pid > 0 and _pid_is_alive(pid):
                raise MigrationBusy("legacy_owner_live")
        if not _valid_legacy_record(record):
            malformed.append((path, raw))
            continue
        valid.append((path, record))
    return valid, malformed


def _valid_legacy_id(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None


def _valid_legacy_attempts(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= 0


def _valid_legacy_body(record: Mapping[str, object]) -> bool:
    """The record names a task type and carries an object payload."""
    if not isinstance(record.get("type"), str) or not record["type"]:
        return False
    return isinstance(record.get("payload"), dict)


def _valid_legacy_record(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    if not _valid_legacy_id(record.get("id")):
        return False
    if not _valid_legacy_body(record):
        return False
    if not _valid_legacy_attempts(record.get("attempts", 0)):
        return False
    return _safe_legacy_timestamp(record.get("enqueued_at")) is not None


def _safe_legacy_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def _import_legacy_record(queue: MemoryQueue, record: dict[str, object], source: Path) -> str:
    created = _safe_legacy_timestamp(record["enqueued_at"])
    if created is None:
        raise QueueOperationError("legacy_invalid")
    last_attempt = _safe_legacy_timestamp(record.get("last_attempt_at"))
    if source.suffix == ".processing":
        last_attempt = _safe_legacy_timestamp(record.get("lease_acquired_at")) or last_attempt
    attempts = int(record.get("attempts", 0))
    payload = _redact_payload(dict(record["payload"]))
    payload_bytes = canonical_json_bytes(payload)
    state = "dead" if attempts >= DEFAULTS.queue_max_attempts else "ready"
    updated = last_attempt or created
    expected = (
        str(record["type"]),
        payload_bytes.decode("utf-8"),
        sha256_bytes(payload_bytes),
        state,
        _timestamp(created),
        _timestamp(updated),
        _timestamp(updated),
        attempts,
        _timestamp(last_attempt) if last_attempt else None,
        "attempts_exhausted" if state == "dead" else None,
    )
    with queue._connect() as connection, begin_immediate(connection):
        connection.execute(
            """INSERT OR IGNORE INTO tasks(
                   id, kind, handler_version, payload_json, input_hash, state, priority,
                   created_at, updated_at, available_at, attempts, last_attempt_at,
                   error_code
               ) VALUES (?, ?, 1, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (
                record["id"],
                *expected,
            ),
        )
        stored = connection.execute(
            """SELECT kind, payload_json, input_hash, state, created_at, updated_at,
                      available_at, attempts, last_attempt_at, error_code
               FROM tasks WHERE id=?""",
            (record["id"],),
        ).fetchone()
        if stored is None or tuple(stored) != expected:
            raise QueueOperationError("legacy_import_conflict")
    return str(record["id"])


def _quarantine_legacy_record(
    run_dir: Path, source: Path, raw: bytes, *, code: str = "legacy_invalid"
) -> None:
    quarantine = run_dir / "queue-quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    _harden_owner_only(quarantine, 0o700)
    digest = sha256_bytes(raw)
    raw_name = f"{digest}.raw"
    _write_durable_file(quarantine / raw_name, raw)
    record = {
        "code": code,
        "raw_name": raw_name,
        "source_name": source.name,
        "source_sha256": digest,
    }
    source_digest = sha256_bytes(source.name.encode("utf-8"))[:16]
    _write_durable_file(
        quarantine / f"{digest}-{source_digest}.json", canonical_json_bytes(record)
    )
    fsync_directory(quarantine)


def _quarantine_legacy_directory(run_dir: Path, legacy_dir: Path, *, code: str) -> int:
    quarantined = 0
    for source in sorted(legacy_dir.iterdir()):
        raw = _read_bounded_regular_nofollow(source)
        _quarantine_legacy_record(run_dir, source, raw, code=code)
        source.unlink()
        quarantined += 1
    legacy_dir.rmdir()
    fsync_directory(run_dir)
    return quarantined


def _post_marker_legacy_conflict(state_root: Path) -> None:
    run_dir, legacy_dir, _db_path, marker = _migration_paths(state_root)
    if not _path_present(marker) or not legacy_dir.exists():
        return
    owner = _acquire_queue_owner(state_root, "legacy", "legacy_owner_busy")
    try:
        if legacy_dir.exists():
            _quarantine_legacy_directory(
                run_dir, legacy_dir, code="legacy_backend_conflict"
            )
            raise LegacyBackendDisabled("legacy_backend_conflict")
    finally:
        _release_queue_owner(owner)


def _legacy_enqueue_file(
    task_type: str, payload: dict[str, Any], state_root: Path | None = None
) -> str:
    root = Path(state_root or _state_root()).resolve()
    _legacy_write_allowed(root)
    owner = _acquire_queue_owner(root, "legacy", "legacy_owner_busy")
    target: Path | None = None
    try:
        _legacy_write_allowed(root)
        task_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        record = {
            "attempts": 0,
            "enqueued_at": _timestamp(_utc_now()),
            "id": task_id,
            "last_attempt_at": None,
            "payload": payload,
            "type": task_type,
        }
        queue_dir = root / "run" / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(queue_dir, 0o700)
        target = queue_dir / f"{task_id}.json"
        owner = _heartbeat_queue_owner(owner)
        marker = _migration_paths(root)[3]
        if _path_present(marker):
            _validate_migration_marker(marker)
            raise LegacyBackendDisabled("legacy_marker_race")
        _write_durable_file(target, canonical_json_bytes(record))
        try:
            owner = _heartbeat_queue_owner(owner)
            marker = _migration_paths(root)[3]
            if _path_present(marker):
                _validate_migration_marker(marker)
                raise LegacyBackendDisabled("legacy_marker_race")
        except Exception:
            target.unlink(missing_ok=True)
            fsync_directory(queue_dir)
            raise
        return task_id
    finally:
        _release_queue_owner(owner)


def _write_durable_file(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _harden_owner_only(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data or not _is_owner_only(path):
                raise QueueOperationError("durable_file_conflict") from None
        else:
            fsync_file(path)
            fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_stable_owner_file(path: Path, max_bytes: int) -> bytes:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > max_bytes
        or not _is_owner_only(path)
    ):
        raise QueueOperationError("export_verification_failed")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (opened.st_dev, opened.st_ino):
            raise QueueOperationError("export_verification_failed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(max_bytes + 1)
        after = os.fstat(descriptor)
        if len(data) > max_bytes or (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise QueueOperationError("export_verification_failed")
        return data
    finally:
        os.close(descriptor)


def _remove_export_staging(staging: Path) -> None:
    if not staging.exists():
        return
    if staging.is_symlink() or not staging.is_dir() or not _is_owner_only(staging):
        raise QueueOperationError("export_staging_invalid")
    shutil.rmtree(staging)
    fsync_directory(staging.parent)


def _cleanup_export_staging(parent: Path, export_name: str) -> None:
    for staging in parent.glob(f".{export_name}.staging-*"):
        _remove_export_staging(staging)


def _commit_migration_marker(
    lease: QueueOwnerLease, marker: Path, run_dir: Path
) -> QueueOwnerLease:
    now = _utc_now()
    with _open_queue_ownership_db(lease.state_root) as connection, begin_immediate(connection):
        expires_at = _require_queue_owner(connection, lease, now, heartbeat=True)
        try:
            _write_durable_file(marker, canonical_json_bytes({"version": 2}))
        except QueueOperationError:
            if _path_present(marker):
                _validate_migration_marker(marker)
            raise
        _validate_migration_marker(marker)
        fsync_directory(run_dir)
    return replace(lease, expires_at=expires_at)


@dataclass
class _MigrationGuard:
    """One migration's deadline, cancellation, and owner heartbeat."""

    deadline: float
    cancelled: Callable[[], bool] | None
    owner: QueueOwnerLease | None = None

    def check(self) -> None:
        if time.monotonic() >= self.deadline or bool(
            self.cancelled and self.cancelled()
        ):
            raise TimeoutError("queue migration cancelled or deadline reached")

    def beat(self) -> None:
        self.owner = _heartbeat_queue_owner(self.owner)


def _migration_already_done(marker: Path, state_root: Path) -> bool:
    """A valid marker means it ran; a legacy directory beside it is a conflict."""
    if not _path_present(marker):
        return False
    _validate_migration_marker(marker)
    _post_marker_legacy_conflict(state_root)
    return True


def _single_migration_input(run_dir: Path, legacy_dir: Path) -> list[Path]:
    """At most one staged migration, and never one beside a live legacy dir."""
    inputs = sorted(run_dir.glob("queue-migration-*"))
    if inputs and legacy_dir.exists():
        raise MigrationBusy("legacy_migration_conflict")
    if len(inputs) > 1:
        raise MigrationBusy("legacy_migration_conflict")
    return inputs


def _migration_input(run_dir: Path, legacy_dir: Path) -> Path | None:
    """The directory this migration consumes, claimed exactly once."""
    inputs = _single_migration_input(run_dir, legacy_dir)
    if inputs:
        return inputs[0]
    if not legacy_dir.exists():
        return None
    claimed = run_dir / f"queue-migration-{uuid.uuid4().hex}"
    legacy_dir.replace(claimed)
    fsync_directory(run_dir)
    return claimed


def _scanned_legacy_records(migration_input: Path | None):
    """The readable and the malformed legacy records, or nothing to migrate."""
    if migration_input is None:
        return [], []
    return _scan_legacy_records(migration_input)


def _import_legacy_records(
    state_root: Path, valid: list[tuple[Path, object]], guard: _MigrationGuard
) -> list[str]:
    """Import every readable legacy record, keeping the owner alive as we go."""
    if not valid:
        return []
    queue = MemoryQueue(Path(state_root))
    imported: list[str] = []
    for source, record in valid:
        guard.check()
        imported.append(_import_legacy_record(queue, record, source))
        source.unlink()
        guard.beat()
    return imported


def _quarantine_legacy_records(
    run_dir: Path, malformed: list[tuple[Path, bytes]], guard: _MigrationGuard
) -> None:
    """Keep every unreadable record as evidence rather than dropping it."""
    for source, raw in malformed:
        guard.check()
        _quarantine_legacy_record(run_dir, source, raw)
        source.unlink(missing_ok=True)
        guard.beat()


def _retire_migration_input(migration_input: Path | None, run_dir: Path) -> None:
    """Remove the consumed input directory, durably."""
    if migration_input is None:
        return
    fsync_directory(migration_input)
    migration_input.rmdir()
    fsync_directory(run_dir)


def _refuse_live_legacy_dir(run_dir: Path, legacy_dir: Path) -> None:
    """A legacy directory that reappeared mid-migration stops it."""
    if not legacy_dir.exists():
        return
    _quarantine_legacy_directory(run_dir, legacy_dir, code="legacy_backend_conflict")
    raise LegacyBackendDisabled("legacy_backend_conflict")


def _run_migration(
    guard: _MigrationGuard,
    state_root: Path,
    run_dir: Path,
    legacy_dir: Path,
    marker: Path,
) -> MigrationReceipt:
    """Consume the staged legacy records, then commit the migration marker."""
    migration_input = _migration_input(run_dir, legacy_dir)
    guard.beat()
    guard.check()
    valid, malformed = _scanned_legacy_records(migration_input)
    guard.beat()
    imported = _import_legacy_records(state_root, valid, guard)
    _quarantine_legacy_records(run_dir, malformed, guard)
    _retire_migration_input(migration_input, run_dir)
    guard.beat()
    _refuse_live_legacy_dir(run_dir, legacy_dir)
    guard.owner = _commit_migration_marker(guard.owner, marker, run_dir)
    codes = ("legacy_invalid",) if malformed else ()
    return MigrationReceipt(len(imported), len(malformed), tuple(imported), codes)


def migrate_legacy_queue(
    state_root: Path,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> MigrationReceipt:
    guard = _MigrationGuard(deadline, cancelled)
    guard.check()
    run_dir, legacy_dir, _db_path, marker = _migration_paths(state_root)
    if _migration_already_done(marker, state_root):
        return MigrationReceipt(0, 0, (), ())
    guard.owner = _acquire_queue_owner(state_root, "migration", "migration_busy")
    legacy_owner: QueueOwnerLease | None = None
    try:
        guard.check()
        if _migration_already_done(marker, state_root):
            return MigrationReceipt(0, 0, (), ())
        legacy_owner = _acquire_queue_owner(state_root, "legacy", "legacy_owner_busy")
        guard.beat()
        guard.check()
        _prove_no_live_processing(legacy_dir)
        guard.beat()
        guard.check()
        return _run_migration(guard, state_root, run_dir, legacy_dir, marker)
    finally:
        if legacy_owner is not None:
            _release_queue_owner(legacy_owner)
        _release_queue_owner(guard.owner)


def _ensure_sqlite_enabled() -> None:
    state_root = _state_root()
    marker = _migration_paths(state_root)[3]
    if not _path_present(marker):
        migrate_legacy_queue(state_root)
    else:
        _validate_migration_marker(marker)
        _post_marker_legacy_conflict(state_root)


def _queue_dir() -> Path:
    """Compatibility path: SQLite and results now live directly under run/."""
    path = _state_root() / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _queue(
    *,
    max_attempts: int = DEFAULTS.queue_max_attempts,
    retry_base_seconds: int = DEFAULTS.retry_base_seconds,
    retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
) -> MemoryQueue:
    _ensure_sqlite_enabled()
    return MemoryQueue(
        _state_root(),
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )


def _v3_queue_for_cli() -> _QueueV3CandidateReader:
    state_root = _state_root()
    return MemoryQueue._from_v3_candidate(
        state_root / "run" / "queue-v3.candidate.sqlite3",
        state_root=state_root,
    )


def active_memory_queue(vault: Path, state_root: Path) -> _QueueV3CandidateReader:
    """Open the queue side of one completely validated adopted V3 pair."""
    from installed_memory_repair import require_reliability_v3_adopted

    resolved_vault = Path(vault).resolve(strict=True)
    state = Path(state_root).absolute()
    require_reliability_v3_adopted(root=resolved_vault, state_root=state)
    queue_path = state / "run" / "queue-v3.sqlite3"
    coordinator_path = state / "run" / "markdown-transactions-v3.sqlite3"
    validate_queue_v3_database(queue_path, state_root=state)
    return _QueueV3CandidateReader(
        queue_path, coordinator_path=coordinator_path
    )


@contextmanager
def _repair_owner_for_cli() -> Iterator[OwnerLease]:
    from operational_ownership import OwnershipRegistry

    registry = OwnershipRegistry(_state_root())
    owner = registry.acquire("repair", scope=f"repair:cli:{os.getpid()}")
    try:
        yield owner
    finally:
        registry.release(owner)


def enqueue(task_type: str, payload: dict[str, Any]) -> str:
    """Compatibility facade that enqueues handler version 1."""
    return _queue().enqueue(task_type, 1, payload)


def _compat_task(task: QueueTask | QueueLease) -> dict[str, Any]:
    created = task.created_at
    last_attempt = task.last_attempt_at
    attempts = task.attempts if isinstance(task, QueueTask) else task.prior_attempts
    return {
        "id": task.id,
        "type": task.kind,
        "handler_version": task.handler_version,
        "enqueued_at": created.isoformat(timespec="seconds"),
        "attempts": attempts,
        "last_attempt_at": (
            last_attempt.isoformat(timespec="seconds") if last_attempt else None
        ),
        "payload": task.payload,
    }


def _as_positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise QueueOperationError(
            "restore_record_invalid", f"{field} is not a positive integer"
        )
    return value


def _as_priority(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return max(-100, min(100, value))


def _restore_json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise QueueOperationError("restore_manifest_invalid", "manifest is not JSON") from None
    if not isinstance(value, dict):
        raise QueueOperationError("restore_manifest_invalid", "manifest is not an object")
    return value


def _verified_export_records(export: Path) -> list[dict[str, object]]:
    """Read one purge export only when its manifest and digests all verify."""
    manifest = _restore_json_object(
        _read_stable_owner_file(export / "manifest.json", _MAX_EXPORT_METADATA_BYTES)
    )
    records_bytes = _read_stable_owner_file(
        export / "records.json", _MAX_EXPORT_METADATA_BYTES
    )
    if sha256_bytes(records_bytes) != manifest.get("records_sha256"):
        raise QueueOperationError("restore_verification_failed", "records digest mismatch")
    _verify_exported_results(export, manifest)
    records = json.loads(records_bytes.decode("utf-8"))
    _require_record_list(records, manifest)
    return records


def _is_object_list(records: object) -> bool:
    if not isinstance(records, list):
        return False
    return all(isinstance(item, dict) for item in records)


def _manifest_task_ids(manifest: Mapping[str, object]) -> list[str]:
    declared = manifest.get("task_ids") or []
    return [str(item) for item in declared]


def _require_record_list(records: object, manifest: Mapping[str, object]) -> None:
    if not _is_object_list(records):
        raise QueueOperationError(
            "restore_manifest_invalid", "records are not a list of objects"
        )
    exported = [str(item.get("id")) for item in records]
    if exported != _manifest_task_ids(manifest):
        raise QueueOperationError("restore_verification_failed", "task ids do not match")


def _verify_exported_results(export: Path, manifest: Mapping[str, object]) -> None:
    for item in manifest.get("results") or []:
        if not isinstance(item, Mapping):
            raise QueueOperationError(
                "restore_manifest_invalid", "result entry is not an object"
            )
        data = _read_stable_owner_file(
            export / "results" / f"{item.get('id')}.result", _MAX_RESULT_BYTES
        )
        if sha256_bytes(data) != item.get("sha256"):
            raise QueueOperationError("restore_verification_failed", "result digest mismatch")


def _export_task_record(task: QueueTask) -> dict[str, object]:
    return {
        "attempt_history": [
            {
                "attempt": item.attempt,
                "error_code": item.error_code,
                "finished_at": _timestamp(item.finished_at),
                "outcome": item.outcome,
                "started_at": _timestamp(item.started_at),
            }
            for item in task.attempt_history
        ],
        "attempts": task.attempts,
        "available_at": _timestamp(task.available_at),
        "blocked_capability": task.blocked_capability,
        "created_at": _timestamp(task.created_at),
        "dedupe_key": task.dedupe_key,
        "error_code": task.error_code,
        "handler_version": task.handler_version,
        "id": task.id,
        "input_hash": task.input_hash,
        "kind": task.kind,
        "last_attempt_at": (
            _timestamp(task.last_attempt_at) if task.last_attempt_at else None
        ),
        "payload": task.payload,
        "priority": task.priority,
        "redrive_of": task.redrive_of,
        "result_reference": task.result_reference,
        "result_sha256": task.result_sha256,
        "state": task.state,
        "updated_at": _timestamp(task.updated_at),
    }


def list_pending(max_age_days: int | None = None) -> list[dict[str, Any]]:
    tasks = _queue().list_tasks(
        states=("ready", "leased", "blocked", "dead"), max_age_days=max_age_days
    )
    return [_compat_task(task) for task in tasks]


def mark_attempt(task_id: str, success: bool) -> None:
    """Legacy attempt API retained for callers while storage is SQLite-backed."""
    queue = _queue()
    try:
        task = queue.get(task_id)
    except KeyError:
        return
    if task.state != "ready":
        return
    lease = queue._claim_task(task_id, f"legacy-{os.getpid()}")
    if lease is None:
        return
    if success:
        queue.publish_result(lease, operation_id=task_id, result=b"")
        queue.acknowledge(lease)
    else:
        queue.fail(lease, QueueFailure("processor_failed", retry_after=60))


def recover_stale_leases(max_age_seconds: int = 600) -> int:
    del max_age_seconds
    return _queue().recover_expired_leases()


def cancel(
    task_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    return _queue().cancel(task_id, deadline=deadline, cancelled=cancelled)


def redrive(
    task_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> str:
    return _queue().redrive(task_id, deadline=deadline, cancelled=cancelled)


def purge(
    *,
    terminal_before: datetime,
    export_path: Path,
    include_dead: bool = False,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> PurgeReceipt:
    return _queue().purge(
        terminal_before=terminal_before,
        export_path=export_path,
        include_dead=include_dead,
        deadline=deadline,
        cancelled=cancelled,
    )


def restore(
    *,
    export_path: Path,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> RestoreReceipt:
    return _queue().restore(
        export_path=export_path,
        deadline=deadline,
        cancelled=cancelled,
    )


def retained_queue_state() -> bool:
    """Return whether queue records or results block deletion of run/."""
    return _queue().retains_run_directory()


class _SourceFenceHeartbeat:
    def __init__(
        self,
        queue: MemoryQueue,
        fence: SourceFence,
        *,
        heartbeat_seconds: int,
        lease_seconds: int,
    ) -> None:
        self._queue = queue
        self._fence = fence
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"memory-source-fence-heartbeat-{fence.daily_id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def current(self) -> SourceFence:
        with self._lock:
            if self.error is not None:
                raise QueueOperationError("source_fence_lost") from self.error
            return self._fence

    def refresh(self) -> SourceFence:
        with self._lock:
            if self.error is not None:
                raise QueueOperationError("source_fence_lost") from self.error
            try:
                self._fence = self._queue.heartbeat_source_fence(
                    self._fence, lease_seconds=self._lease_seconds
                )
            except Exception as exc:
                self.error = exc
                self._stop.set()
                raise QueueOperationError("source_fence_lost") from exc
            return self._fence

    def _run(self) -> None:
        while not self._queue._heartbeat_wait(  # noqa: SLF001 - injected queue seam
            self._stop, self._heartbeat_seconds
        ):
            try:
                self.refresh()
            except QueueOperationError:
                return


class _LeaseHeartbeat:
    def __init__(
        self,
        queue: MemoryQueue,
        lease: QueueLease,
        *,
        heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> None:
        self._queue = queue
        self._lease = lease
        self._heartbeat_seconds = heartbeat_seconds
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self.error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"memory-queue-heartbeat-{lease.id}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        while not self._queue._heartbeat_wait(  # noqa: SLF001 - injected queue seam
            self._stop, self._heartbeat_seconds
        ):
            try:
                self._lease = self._queue.heartbeat(
                    self._lease, lease_seconds=self._lease_seconds
                )
            except Exception as exc:  # noqa: BLE001 - completion must remain fenced
                self.error = exc
                self._stop.set()
                return


def drain_with(
    processor: Callable[[dict], bool | DeferredResult], max_tasks: int = 10
) -> dict[str, int]:
    """Run handlers outside transactions and fence completion with a result marker."""
    counts = {"ok": 0, "failed": 0, "dead": 0, "skipped": 0}
    queue = _queue()
    owner = f"compat-{os.getpid()}-{uuid.uuid4().hex}"
    for _ in range(max_tasks):
        lease = queue.claim(owner)
        if lease is None:
            break
        _drain_one_lease(queue, processor, lease, counts)
    return counts


def _drain_one_lease(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    lease: QueueLease,
    counts: dict[str, int],
) -> None:
    """Adopt, run and settle one leased task."""
    adopted = queue.adopt_published_result(lease, operation_id=lease.id)
    if adopted == "adopted":
        _count_terminal(counts, queue.acknowledge(lease))
        return
    if adopted == "corrupt":
        _count_terminal(counts, queue.get(lease.id))
        return
    outcome, heartbeat_lost = _run_compat_processor(queue, processor, lease)
    if heartbeat_lost:
        counts["failed"] += 1
        return
    _settle_compat_outcome(queue, lease, outcome, counts)


def _run_compat_processor(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    lease: QueueLease,
) -> tuple[bool | DeferredResult, bool]:
    """(what the processor returned, whether the lease heartbeat was lost)."""
    heartbeat = _LeaseHeartbeat(queue, lease)
    heartbeat.start()
    try:
        outcome: bool | DeferredResult = _compat_processor_outcome(processor, lease)
    finally:
        heartbeat.stop()
    return outcome, heartbeat.error is not None


def _compat_processor_outcome(
    processor: Callable[[dict], bool | DeferredResult], lease: QueueLease
) -> bool | DeferredResult:
    """Run the handler; a raised exception is a plain failure here."""
    try:
        return processor(_compat_task(lease))
    except Exception:  # noqa: BLE001 - a compat handler may raise anything
        print("processor_exception", file=sys.stderr)
        return False


def _settle_compat_outcome(
    queue: MemoryQueue,
    lease: QueueLease,
    outcome: bool | DeferredResult,
    counts: dict[str, int],
) -> None:
    """Publish or fail the task, counting a lost fence as a failure."""
    try:
        _publish_compat_outcome(queue, lease, outcome, counts)
    except (LeaseFenceError, ResultConflictError):
        counts["failed"] += 1
        if queue.get(lease.id).state == "dead":
            counts["dead"] += 1


def _publish_compat_outcome(
    queue: MemoryQueue,
    lease: QueueLease,
    outcome: bool | DeferredResult,
    counts: dict[str, int],
) -> None:
    if not outcome:
        queue.fail(lease, QueueFailure("processor_failed", retry_after=60))
        _count_terminal(counts, queue.get(lease.id))
        return
    result = outcome.data if isinstance(outcome, DeferredResult) else b""
    queue.publish_result(lease, operation_id=lease.id, result=result)
    _count_terminal(counts, queue.acknowledge(lease))


def _run_processor_inline(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
) -> bool | DeferredResult:
    del timeout
    return processor(task)


def _processor_child_entry(
    sender: Any,
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
) -> None:
    try:
        if os.name != "nt":
            os.setsid()
        try:
            outcome = processor(task)
            if isinstance(outcome, DeferredResult):
                frame = b"D" + outcome.data
            elif isinstance(outcome, bool):
                frame = b"T" if outcome else b"F"
            else:
                frame = b"?"
        except Exception:  # noqa: BLE001 - parent receives a stable code only
            frame = b"E"
        sender.send_bytes(b"R")
        if not sender.poll(5) or sender.recv_bytes(1) != b"A":
            return
        sender.send_bytes(frame)
    except Exception:  # noqa: BLE001 - parent receives a stable code only
        pass
    finally:
        sender.close()


def _kill_process_group(pid: int, sig: int) -> None:
    os.killpg(pid, sig)


def _process_snapshot_posix() -> list[tuple[int, int, int, str]] | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    rows: list[tuple[int, int, int, str]] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii")
            fields = raw[raw.rfind(")") + 2 :].split()
            rows.append((int(entry.name), int(fields[1]), int(fields[2]), fields[0]))
        except (OSError, ValueError, IndexError):
            continue
    return rows


_TH32CS_SNAPPROCESS = 0x2
_MAX_PROCESS_PATH = 260


def _process_entry_type():
    """The PROCESSENTRY32 layout, built only when ctypes has Windows types."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            # ULONG_PTR: a pointer keeps the right width on 32- and 64-bit.
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * _MAX_PROCESS_PATH),
        ]

    return ProcessEntry32


def _walk_process_snapshot(kernel32, snapshot) -> list[tuple[int, int]]:
    import ctypes

    entry = _process_entry_type()()
    entry.dwSize = ctypes.sizeof(entry)
    pairs: list[tuple[int, int]] = []
    ok = kernel32.Process32First(snapshot, ctypes.byref(entry))
    while ok:
        pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
        ok = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    return pairs


def _windows_process_pairs() -> list[tuple[int, int]] | None:
    """Every (pid, parent pid) from the kernel32 process snapshot.

    The only source: it is present on every supported Windows, needs no
    subprocess, and answers in milliseconds. None means the snapshot failed,
    which callers read as an unknown tree rather than a clean one.
    """
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snapshot in (None, -1, ctypes.c_void_p(-1).value):
            return None
        try:
            return _walk_process_snapshot(kernel32, snapshot)
        finally:
            kernel32.CloseHandle(snapshot)
    except (AttributeError, OSError, ValueError):
        return None


def _descendants_of(root_pid: int, pairs: list[tuple[int, int]]) -> set[int]:
    descendants: set[int] = set()
    frontier = {root_pid}
    while frontier:
        children = {
            pid for pid, ppid in pairs if ppid in frontier and pid not in descendants
        }
        descendants.update(children)
        frontier = children
    return descendants


def _posix_process_pairs() -> list[tuple[int, int]]:
    snapshot = _process_snapshot_posix()
    if snapshot is None:
        return []
    return [(pid, ppid) for pid, ppid, _pgrp, _state in snapshot]


def _tracked_descendant_pids(root_pid: int, platform_name: str) -> set[int] | None:
    if platform_name != "nt":
        return _descendants_of(root_pid, _posix_process_pairs())
    pairs = _windows_process_pairs()
    if pairs is None:
        return None
    return _descendants_of(root_pid, pairs)


def _process_group_alive(group_id: int) -> bool:
    snapshot = _process_snapshot_posix()
    if snapshot is not None:
        return any(pgrp == group_id and state != "Z" for _pid, _ppid, pgrp, state in snapshot)
    try:
        _kill_process_group(group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cleanup_confirmed(
    process: multiprocessing.Process,
    descendants: set[int],
    *,
    platform_name: str,
) -> bool:
    snapshot = _process_snapshot_posix() if platform_name != "nt" else None
    if snapshot is None:
        descendant_alive = any(_pid_is_alive(pid) for pid in descendants)
    else:
        states = {pid: state for pid, _ppid, _pgrp, state in snapshot}
        descendant_alive = any(
            pid in states and states[pid] != "Z" for pid in descendants
        )
    if process.is_alive() or descendant_alive:
        return False
    return platform_name == "nt" or not _process_group_alive(process.pid)


_DISCOVER_DESCENDANTS = object()


def _require_windows_child_alive(
    process: multiprocessing.Process,
    platform_name: str,
    discover_descendants: bool,
    direct_was_alive: bool,
) -> None:
    """Windows cannot enumerate the descendants of a process that is already gone."""
    if direct_was_alive or platform_name != "nt" or not discover_descendants:
        return
    raise QueueOperationError(
        "process_cleanup_failed",
        f"child exited {getattr(process, 'exitcode', None)} before cleanup",
    )


def _discovered_descendants(
    process: multiprocessing.Process,
    platform_name: str,
    discover: bool,
    tracked: set[int] | None | object,
):
    if not discover:
        return tracked
    return _tracked_descendant_pids(process.pid, platform_name)


def _taskkill(pid: int) -> bool:
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _kill_windows_tree(
    process: multiprocessing.Process, descendants: set[int] | None
) -> bool:
    tree_verified = _taskkill(process.pid)
    if descendants is None:
        return tree_verified
    for pid in descendants:
        if _pid_is_alive(pid):
            _taskkill(pid)
    return tree_verified


def _kill_posix_group(
    process: multiprocessing.Process, descendants: set[int] | None
) -> bool:
    try:
        _kill_process_group(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return os.name != "nt" and descendants == set()
    except OSError:
        return False
    return True


def _kill_process_tree(
    process: multiprocessing.Process,
    descendants: set[int] | None,
    platform_name: str,
) -> bool:
    if platform_name == "nt":
        return _kill_windows_tree(process, descendants)
    return _kill_posix_group(process, descendants)


def _hard_kill_group(
    process: multiprocessing.Process, platform_name: str, tree_verified: bool
) -> bool:
    if not process.is_alive() or platform_name == "nt" or not tree_verified:
        return tree_verified
    try:
        _kill_process_group(process.pid, signal.SIGKILL)
    except OSError:
        tree_verified = False
    process.join(0.2)
    return tree_verified


def _stop_quietly(process: multiprocessing.Process, action) -> None:
    if not process.is_alive():
        return
    try:
        action()
    except (OSError, ValueError):
        pass
    process.join(0.2)


def _escalate_termination(
    process: multiprocessing.Process, platform_name: str, tree_verified: bool
) -> bool:
    process.join(0.2)
    tree_verified = _hard_kill_group(process, platform_name, tree_verified)
    _stop_quietly(process, process.terminate)
    _stop_quietly(process, process.kill)
    return tree_verified


def _await_cleanup(
    process: multiprocessing.Process,
    descendants: set[int] | None,
    platform_name: str,
    cleanup_timeout: float,
) -> bool:
    if descendants is None:
        return False
    deadline = time.monotonic() + max(0.0, cleanup_timeout)
    while True:
        if _cleanup_confirmed(process, descendants, platform_name=platform_name):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _cleanup_failed(
    cleanup_verified: bool, platform_name: str, tree_verified: bool
) -> bool:
    if not cleanup_verified:
        return True
    return platform_name != "nt" and not tree_verified


def _terminate_processor_child(
    process: multiprocessing.Process,
    *,
    platform_name: str | None = None,
    # A terminated Windows tree can take a long time to disappear on a loaded
    # machine, and the worker then reports `process_cleanup_failed` for a
    # cleanup that was merely still in progress. One second was not enough,
    # and neither was fifteen on a hosted four-vCPU runner. The wait is sized
    # for the slowest supported machine and stays bounded; the happy path
    # returns as soon as the tree is gone.
    cleanup_timeout: float = 60.0,
    tracked_descendants: set[int] | None | object = _DISCOVER_DESCENDANTS,
) -> set[int]:
    platform_name = platform_name or os.name
    direct_was_alive = process.is_alive()
    discover_descendants = tracked_descendants is _DISCOVER_DESCENDANTS
    _require_windows_child_alive(
        process, platform_name, discover_descendants, direct_was_alive
    )
    descendants = _discovered_descendants(
        process, platform_name, discover_descendants, tracked_descendants
    )
    if descendants is not None and _cleanup_confirmed(
        process, descendants, platform_name=platform_name
    ):
        return descendants
    tree_verified = _kill_process_tree(process, descendants, platform_name)
    tree_verified = _escalate_termination(process, platform_name, tree_verified)
    cleanup_verified = _await_cleanup(
        process, descendants, platform_name, cleanup_timeout
    )
    if _cleanup_failed(cleanup_verified, platform_name, tree_verified):
        raise QueueOperationError("process_cleanup_failed")
    return descendants


def _decode_processor_frame(frame: bytes) -> bool | DeferredResult:
    if not frame:
        raise QueueOperationError("processor_result_malformed")
    tag = frame[:1]
    if tag == b"D":
        data = frame[1:]
        if len(data) > _MAX_RESULT_BYTES:
            raise QueueOperationError("processor_result_oversize")
        return DeferredResult(data)
    if frame == b"T":
        return True
    if frame == b"F":
        return False
    if frame == b"E":
        raise QueueOperationError("processor_exception")
    raise QueueOperationError("processor_result_malformed")


@dataclass
class _ChildRun:
    """One processor child: its process, its pipe, and its deadline."""

    process: multiprocessing.Process
    receiver: object
    deadline: float
    tracked_descendants: set[int] | None = None

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def stop(self) -> None:
        """Terminate the child, discovering its tree when we never tracked it.

        Passing an explicit None means "there are no descendants"; only the
        default sentinel asks for discovery, and before the handshake that is
        what we want.
        """
        tracked = self.tracked_descendants
        if tracked is None:
            self.tracked_descendants = _terminate_processor_child(self.process)
            return
        self.tracked_descendants = _terminate_processor_child(
            self.process, tracked_descendants=tracked
        )


def _await_child_message(run: _ChildRun) -> None:
    """Wait for the child to say something before its deadline runs out."""
    remaining = run.remaining()
    if remaining <= 0 or not run.receiver.poll(remaining):
        run.stop()
        raise TimeoutError


def _child_ready_handshake(run: _ChildRun) -> None:
    """The child announces it is up, and only then do we track its tree."""
    _await_child_message(run)
    try:
        ready = run.receiver.recv_bytes(1)
    except (OSError, EOFError):
        raise QueueOperationError("processor_result_malformed") from None
    if ready != b"R":
        raise QueueOperationError("processor_result_malformed")
    run.tracked_descendants = _tracked_descendant_pids(run.process.pid, os.name)
    run.receiver.send_bytes(b"A")


def _abandon_oversize_child(run: _ChildRun) -> None:
    """A child that sent more than we accept is stopped, not waited out."""
    run.receiver.close()
    run.process.join(0.5)
    if run.process.is_alive():
        run.stop()


def _child_result_frame(run: _ChildRun) -> bytes:
    """The result frame the child sent, or why it could not be read."""
    _await_child_message(run)
    try:
        return run.receiver.recv_bytes(_MAX_RESULT_BYTES + 1)
    except OSError:
        _abandon_oversize_child(run)
        raise QueueOperationError("processor_result_oversize") from None
    except EOFError:
        raise QueueOperationError("processor_result_malformed") from None


def _join_child(run: _ChildRun) -> None:
    """Wait out the child's exit within the deadline, and check how it left."""
    remaining = run.remaining()
    if remaining <= 0:
        run.stop()
        raise TimeoutError
    run.process.join(remaining)
    if run.process.is_alive():
        run.stop()
        raise TimeoutError
    if run.process.exitcode != 0:
        raise QueueOperationError("processor_child_failed")


def _run_processor_child(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
) -> bool | DeferredResult:
    if timeout <= 0:
        raise TimeoutError
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=True)
    process = context.Process(
        target=_processor_child_entry,
        args=(sender, processor, task),
        daemon=False,
    )
    run = _ChildRun(process, receiver, time.monotonic() + timeout)
    started = False
    try:
        process.start()
        started = True
        sender.close()
        _child_ready_handshake(run)
        frame = _child_result_frame(run)
        _join_child(run)
        return _decode_processor_frame(frame)
    finally:
        receiver.close()
        if started:
            _terminate_processor_child(
                process, tracked_descendants=run.tracked_descendants
            )


_CLAIM_BUSY_RETRY_SECONDS = 0.05


def _is_busy_database(error: sqlite3.OperationalError) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(error).casefold()
    return "busy" in message or "locked" in message


def _claim_when_reachable(
    queue: MemoryQueue,
    *,
    owner: str,
    lease_seconds: int,
    max_attempts: int,
    deadline: float,
    monotonic: Callable[[], float],
) -> QueueLease | None:
    """A busy database means "try again shortly", not a dead worker.

    Another worker holding the write lock used to surface as an unhandled
    `database is locked` out of the worker loop, which ended the run.
    """
    while True:
        try:
            return queue.claim(owner, lease_seconds=lease_seconds, max_attempts=max_attempts)
        except sqlite3.OperationalError as error:
            if not _is_busy_database(error) or monotonic() >= deadline:
                return None
            time.sleep(_CLAIM_BUSY_RETRY_SECONDS)


class _ProcessorOutcome(NamedTuple):
    outcome: bool | DeferredResult
    timed_out: bool
    cleanup_failed: bool


def _adopted_counts(
    queue: MemoryQueue, lease: QueueLease, counts: dict[str, int]
) -> bool:
    """True when a previously published result settled this lease already."""
    adopted = queue.adopt_published_result(lease, operation_id=lease.id)
    if adopted == "adopted":
        _count_terminal(counts, queue.acknowledge(lease))
        return True
    if adopted == "corrupt":
        _count_terminal(counts, queue.get(lease.id))
        return True
    return False


def _run_processor(
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    lease: QueueLease,
    remaining: float,
) -> _ProcessorOutcome:
    try:
        outcome = processor_runner(processor, _compat_task(lease), remaining)
    except TimeoutError:
        return _ProcessorOutcome(False, True, False)
    except QueueOperationError as exc:
        return _ProcessorOutcome(False, False, exc.code == "process_cleanup_failed")
    except Exception:  # noqa: BLE001 - queue exposes stable codes only
        return _ProcessorOutcome(False, False, False)
    return _ProcessorOutcome(outcome, False, False)


def _processor_result(
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    lease: QueueLease,
    heartbeat: _LeaseHeartbeat,
    deadline: float,
    monotonic: Callable[[], float],
) -> _ProcessorOutcome:
    heartbeat.start()
    try:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _ProcessorOutcome(False, True, False)
        result = _run_processor(processor, processor_runner, lease, remaining)
        if monotonic() >= deadline:
            return _ProcessorOutcome(result.outcome, True, result.cleanup_failed)
        return result
    finally:
        heartbeat.stop()


def _fail_lease(
    queue: MemoryQueue,
    lease: QueueLease,
    failure: QueueFailure,
    counts: dict[str, int],
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    queue.fail(
        lease,
        failure,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )
    _count_terminal(counts, queue.get(lease.id))


def _settle_processor_outcome(
    queue: MemoryQueue,
    lease: QueueLease,
    result: _ProcessorOutcome,
    counts: dict[str, int],
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    bounds = {
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
        "retry_cap_seconds": retry_cap_seconds,
    }
    if result.cleanup_failed:
        counts["halted"] = 1
        failure = QueueFailure(
            "process_cleanup_failed", blocked_capability="process_cleanup"
        )
        _fail_lease(queue, lease, failure, counts, **bounds)
        return
    if result.timed_out:
        _fail_lease(queue, lease, QueueFailure("worker_timeout"), counts, **bounds)
        return
    if not bool(result.outcome):
        _fail_lease(queue, lease, QueueFailure("processor_failed"), counts, **bounds)
        return
    data = b""
    if isinstance(result.outcome, DeferredResult):
        data = result.outcome.data
    queue.publish_result(lease, operation_id=lease.id, result=data)
    _count_terminal(counts, queue.acknowledge(lease))


def _work_one(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    *,
    owner: str,
    deadline: float,
    monotonic: Callable[[], float],
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> dict[str, int]:
    counts = {"ok": 0, "failed": 0, "dead": 0, "skipped": 0, "halted": 0}
    lease = _claim_when_reachable(
        queue,
        owner=owner,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
        deadline=deadline,
        monotonic=monotonic,
    )
    if lease is None:
        return counts
    if _adopted_counts(queue, lease, counts):
        return counts
    heartbeat = _LeaseHeartbeat(
        queue,
        lease,
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
    )
    result = _processor_result(
        processor, processor_runner, lease, heartbeat, deadline, monotonic
    )
    if heartbeat.error is not None and not result.cleanup_failed:
        counts["failed"] += 1
        return counts
    try:
        _settle_processor_outcome(
            queue,
            lease,
            result,
            counts,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds,
        )
    except (LeaseFenceError, ResultConflictError):
        counts["failed"] += 1
        if queue.get(lease.id).state == "dead":
            counts["dead"] += 1
    return counts


@dataclass
class _WorkerProgress:
    """What a worker has done, and when it last had something to do."""

    totals: dict[str, int]
    processed: int = 0
    idle_started: float | None = None

    def advance(self, counts: Mapping[str, int]) -> str:
        """`worked`, `halted` or `idle` after recording one turn."""
        handled = counts["ok"] + counts["failed"]
        for key in self.totals:
            self.totals[key] += counts[key]
        if not handled:
            return "idle"
        self.processed += handled
        self.idle_started = None
        return "halted" if counts.get("halted") else "worked"


def _worker_exhausted(
    *,
    cancelled: Callable[[], bool] | None,
    now: float,
    deadline: float,
    idle_started: float | None,
    idle_seconds: int,
) -> bool:
    """The worker has run out of time, permission, or patience."""
    if cancelled is not None and cancelled():
        return True
    if now >= deadline:
        return True
    return idle_started is not None and now - idle_started >= idle_seconds


def _worker_idle_pause(
    progress: _WorkerProgress,
    *,
    now: float,
    idle_seconds: int,
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Sleep out part of the idle window; False when the budget is gone."""
    idle_remaining = idle_seconds - (now - progress.idle_started)
    budget_remaining = deadline - monotonic()
    if budget_remaining <= 0:
        return False
    sleep(min(1.0, idle_remaining, budget_remaining))
    return True


def _worker_idle_step(
    progress: _WorkerProgress,
    *,
    now: float,
    idle_seconds: int,
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Wait out one idle turn; False when the worker should stop."""
    if idle_seconds == 0:
        return False
    if progress.idle_started is None:
        progress.idle_started = now
    return _worker_idle_pause(
        progress,
        now=now,
        idle_seconds=idle_seconds,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )


def _worker_turn(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[..., bool | DeferredResult],
    progress: _WorkerProgress,
    *,
    now: float,
    owner: str,
    deadline: float,
    idle_seconds: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    policy: Mapping[str, int],
) -> bool:
    """One turn of the worker loop; False when it should stop."""
    counts = _work_one(
        queue,
        processor,
        processor_runner,
        owner=owner,
        deadline=deadline,
        monotonic=monotonic,
        **policy,
    )
    outcome = progress.advance(counts)
    if outcome == "halted":
        return False
    if outcome == "worked":
        return True
    return _worker_idle_step(
        progress,
        now=now,
        idle_seconds=idle_seconds,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )


def _drive_worker(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[..., bool | DeferredResult],
    progress: _WorkerProgress,
    *,
    owner: str,
    deadline: float,
    max_tasks: int,
    idle_seconds: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    cancelled: Callable[[], bool] | None,
    policy: Mapping[str, int],
) -> None:
    while progress.processed < max_tasks:
        now = monotonic()
        if _worker_exhausted(
            cancelled=cancelled,
            now=now,
            deadline=deadline,
            idle_started=progress.idle_started,
            idle_seconds=idle_seconds,
        ):
            return
        if not _worker_turn(
            queue,
            processor,
            processor_runner,
            progress,
            now=now,
            owner=owner,
            deadline=deadline,
            idle_seconds=idle_seconds,
            monotonic=monotonic,
            sleep=sleep,
            policy=policy,
        ):
            return


def run_worker(
    processor: Callable[[dict], bool | DeferredResult],
    *,
    max_tasks: int = DEFAULTS.worker_max_tasks,
    max_seconds: int = DEFAULTS.worker_max_seconds,
    idle_seconds: int = DEFAULTS.worker_idle_seconds,
    lease_seconds: int = DEFAULTS.queue_lease_seconds,
    heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
    max_attempts: int = DEFAULTS.queue_max_attempts,
    retry_base_seconds: int = DEFAULTS.retry_base_seconds,
    retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ] = _run_processor_child,
    cancelled: Callable[[], bool] | None = None,
) -> WorkerSummary:
    """Run a short-lived worker bounded by tasks, wall time, and idle time."""
    if max_tasks < 0 or max_seconds < 0 or idle_seconds < 0:
        raise ValueError("worker limits must be non-negative")
    policy = {
        "lease_seconds": lease_seconds,
        "heartbeat_seconds": heartbeat_seconds,
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
        "retry_cap_seconds": retry_cap_seconds,
    }
    _validate_worker_policy(
        lease_seconds,
        heartbeat_seconds,
        max_attempts,
        retry_base_seconds,
        retry_cap_seconds,
    )
    progress = _WorkerProgress({"ok": 0, "failed": 0, "dead": 0, "skipped": 0})
    queue = _queue(
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )
    _drive_worker(
        queue,
        processor,
        processor_runner,
        progress,
        owner=f"worker-{os.getpid()}-{uuid.uuid4().hex}",
        deadline=monotonic() + max_seconds,
        max_tasks=max_tasks,
        idle_seconds=idle_seconds,
        monotonic=monotonic,
        sleep=sleep,
        cancelled=cancelled,
        policy=policy,
    )
    return WorkerSummary(
        progress.processed,
        progress.totals["ok"],
        progress.totals["failed"],
        progress.totals["dead"],
        progress.totals["skipped"],
        queue.count_eligible(max_attempts=max_attempts),
    )


def _count_terminal(counts: dict[str, int], task: QueueTask) -> None:
    if task.state == "succeeded":
        counts["ok"] += 1
        return
    counts["failed"] += 1
    if task.state == "dead":
        counts["dead"] += 1


def status() -> dict[str, Any]:
    queue = _queue()
    tasks = queue.list_tasks(states=("ready", "leased", "blocked", "dead"))
    by_type: dict[str, int] = {}
    for task in tasks:
        by_type[task.kind] = by_type.get(task.kind, 0) + 1
    return {
        "pending_total": len(tasks),
        "by_type": by_type,
        "permanently_failed": sum(task.state == "dead" for task in tasks),
        "queue_dir": str(queue.run_dir),
    }


def _operator_status() -> dict[str, object]:
    tasks = _queue().list_tasks()
    states = {state: 0 for state in _STATES}
    capabilities: set[str] = set()
    codes: set[str] = set()
    for task in tasks:
        states[task.state] += 1
        if task.blocked_capability:
            capabilities.add(task.blocked_capability)
        if task.error_code:
            codes.add(task.error_code)
    return {
        "counts": {"total": len(tasks)},
        "states": states,
        "capabilities": sorted(capabilities),
        "codes": sorted(codes),
    }


def _manual_query(payload: Mapping[str, Any]) -> bool | DeferredResult:
    """Answer one deferred query with the configured backend."""
    from llm_client import call_llm

    prompt = payload.get("prompt", "")
    if not prompt:
        return False
    result = call_llm(
        prompt,
        payload.get("system_prompt", ""),
        max_tokens=int(payload.get("max_tokens") or 4000),
    )
    if not result:
        return False
    return DeferredResult(result.encode("utf-8"))


def _valid_day(raw_day: object, now: datetime) -> str | None:
    """The day this flush belongs to, when it is a real calendar date."""
    day = now.strftime("%Y-%m-%d") if raw_day is None else raw_day
    if not isinstance(day, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
        return None
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    return day if parsed.strftime("%Y-%m-%d") == day else None


def _daily_log_path(day: str) -> Path | None:
    """The daily log for this day, when it stays inside the daily directory."""
    root = Path(os.environ.get("LLM_WIKI_ROOT", ".")).resolve()
    daily_dir = (root / "knowledge" / "daily").resolve()
    daily_path = (daily_dir / f"{day}.md").resolve()
    try:
        daily_path.relative_to(daily_dir)
    except ValueError:
        return None
    return daily_path


def _append_flush_block(
    daily_path: Path,
    payload: Mapping[str, Any],
    task_id: str,
    result: str,
    now: datetime,
) -> None:
    """Append what the classifier judged worth keeping, once per task."""
    from daily_log_append import locked_append_once
    from flush_memory import _classify_response

    tier, body = _classify_response(result)
    if tier == "ok" or not body:
        return
    session_id = payload.get("session_id", "deferred")
    event = payload.get("event", "session-end")
    block = (
        f"\n## [{now.strftime('%H:%M:%S')}] deferred-{event} | {session_id}\n"
        f"- Tier: `{tier}`\n\n{redact_secrets(body)}\n"
    )
    locked_append_once(daily_path, block, task_id)


def _flush_target_path(payload: Mapping[str, Any], now: datetime) -> Path | None:
    """The daily log this flush may append to, when the day names one."""
    day = _valid_day(payload.get("day"), now)
    if day is None:
        return None
    return _daily_log_path(day)


def _manual_flush(task: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Summarize one session into its daily log."""
    from llm_client import call_llm

    prompt = payload.get("prompt", "")
    if not prompt:
        return False
    now = _utc_now()
    daily_path = _flush_target_path(payload, now)
    if daily_path is None:
        return False
    result = call_llm(
        prompt,
        payload.get("system_prompt", ""),
        max_tokens=int(payload.get("max_tokens") or 1500),
    )
    if not result:
        return False
    _append_flush_block(daily_path, payload, str(task["id"]), result, now)
    return True


def _manual_compile() -> bool:
    """Run one compile pass in a child process."""
    root = Path(os.environ.get("LLM_WIKI_ROOT", ".")).resolve()
    command = [
        sys.executable,
        str(root / "scripts" / "compile_memory.py"),
        "--trigger",
        "auto",
    ]
    completed = subprocess.run(
        command,
        cwd=root,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _manual_processor(task: dict[str, Any]) -> bool | DeferredResult:
    task_type = task.get("type")
    payload = task.get("payload", {})
    if task_type == "query":
        return _manual_query(payload)
    if task_type == "flush":
        return _manual_flush(task, payload)
    if task_type == "compile":
        return _manual_compile()
    return False


def _cli_error_code(error: BaseException) -> str:
    if isinstance(error, QueueOperationError):
        code = error.code
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            return code
        return "internal_error"
    if isinstance(error, KeyError):
        return "task_not_found"
    if isinstance(error, PermissionError):
        return "permission_denied"
    if isinstance(error, sqlite3.Error):
        return "sqlite_error"
    if isinstance(error, (UnicodeError, json.JSONDecodeError)):
        return "decode_error"
    if isinstance(
        error,
        (
            subprocess.SubprocessError,
            multiprocessing.ProcessError,
            BrokenPipeError,
            EOFError,
            ChildProcessError,
        ),
    ):
        return "process_error"
    if isinstance(error, TimeoutError):
        return "worker_timeout"
    if isinstance(error, OSError):
        return "os_error"
    if isinstance(error, (TypeError, ValueError)):
        return "invalid_input"
    return "internal_error"


def _emit_cli_error(error: BaseException) -> int:
    payload = canonical_json_bytes({"codes": [_cli_error_code(error)]})
    print(payload.decode("utf-8"))
    return 2


def _emit_invalid_arguments() -> int:
    payload = canonical_json_bytes({"ok": False, "code": "invalid_arguments"})
    print(payload.decode("utf-8"))
    return 2


def _build_cli_parser() -> _RedactedArgumentParser:
    parser = _RedactedArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "list",
            "status",
            "work",
            "cancel",
            "redrive",
            "migrate",
            "purge",
            "restore",
            "quarantine-corrupt",
            "purge-corrupt",
        ],
    )
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--max-tasks", type=int, default=DEFAULTS.worker_max_tasks)
    parser.add_argument("--max-seconds", type=int, default=DEFAULTS.worker_max_seconds)
    parser.add_argument("--idle-seconds", type=int, default=DEFAULTS.worker_idle_seconds)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULTS.queue_lease_seconds)
    parser.add_argument(
        "--heartbeat-seconds", type=int, default=DEFAULTS.queue_heartbeat_seconds
    )
    parser.add_argument("--max-attempts", type=int, default=DEFAULTS.queue_max_attempts)
    parser.add_argument(
        "--retry-base-seconds", type=int, default=DEFAULTS.retry_base_seconds
    )
    parser.add_argument(
        "--retry-cap-seconds", type=int, default=DEFAULTS.retry_cap_seconds
    )
    parser.add_argument("--terminal-before")
    parser.add_argument("--export", type=Path)
    parser.add_argument(
        "--include-dead",
        action="store_true",
        help="Also purge attempts-exhausted tasks; they are retained by default.",
    )
    parser.add_argument("--reason")
    return parser


def _cli_list(_args, _parser) -> int:
    records = [{"id": task.id, "state": task.state} for task in _queue().list_tasks()]
    print(json.dumps(records, sort_keys=True))
    return 0


def _cli_status(_args, _parser) -> int:
    print(json.dumps(_operator_status(), sort_keys=True))
    return 0


def _cli_work(args, parser) -> int:
    try:
        _validate_worker_policy(
            args.lease_seconds,
            args.heartbeat_seconds,
            args.max_attempts,
            args.retry_base_seconds,
            args.retry_cap_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))
    summary = run_worker(
        _manual_processor,
        max_tasks=args.max_tasks,
        max_seconds=args.max_seconds,
        idle_seconds=args.idle_seconds,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        max_attempts=args.max_attempts,
        retry_base_seconds=args.retry_base_seconds,
        retry_cap_seconds=args.retry_cap_seconds,
    )
    counts = {
        "dead": summary.dead,
        "failed": summary.failed,
        "processed": summary.processed,
        "remaining_eligible": summary.remaining_eligible,
        "skipped": summary.skipped,
        "succeeded": summary.succeeded,
    }
    print(json.dumps({"counts": counts}, sort_keys=True))
    if summary.failed or summary.dead or summary.remaining_eligible:
        return 1
    return 0


def _cli_migrate(_args, _parser) -> int:
    receipt = migrate_legacy_queue(_state_root())
    print(
        json.dumps(
            {
                "codes": list(receipt.codes),
                "counts": {
                    "imported": receipt.imported,
                    "quarantined": receipt.quarantined,
                },
            },
            sort_keys=True,
        )
    )
    return 0


def _require_cli_task_id(args) -> str:
    if not args.task_id:
        raise QueueOperationError("task_id_required")
    return str(args.task_id)


def _cli_quarantine_corrupt(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    if not isinstance(args.reason, str):
        raise ValueError("quarantine reason is invalid")
    if not 1 <= len(args.reason.encode("utf-8")) <= 4096:
        raise ValueError("quarantine reason is invalid")
    queue = _v3_queue_for_cli()
    with _repair_owner_for_cli() as owner:
        progress = queue.quarantine_corrupt(task_id, reason=args.reason, owner=owner)
    print(
        json.dumps(
            {
                "code": progress.code,
                "complete": progress.complete,
                "operation_id": progress.operation_id,
                "page_count": progress.pages_written,
                "state": progress.state,
                "task_id": progress.task_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_purge_corrupt(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    queue = _v3_queue_for_cli()
    with _repair_owner_for_cli() as owner:
        progress = queue.purge_quarantined(task_id, owner=owner)
    print(
        json.dumps(
            {
                "code": progress.code,
                "complete": progress.complete,
                "links_deleted": progress.links_deleted,
                "operation_id": progress.operation_id,
                "page_count": progress.pages_written,
                "state": progress.state,
                "task_id": progress.task_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_cancel(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    changed = cancel(task_id)
    state = "unchanged"
    if changed:
        state = _queue().get(task_id).state
    print(json.dumps({"id": task_id, "state": state}, sort_keys=True))
    if changed:
        return 0
    return 1


def _cli_redrive(args, _parser) -> int:
    task_id = redrive(_require_cli_task_id(args))
    print(json.dumps({"id": task_id, "state": "ready"}, sort_keys=True))
    return 0


def _cli_terminal_before(args) -> datetime:
    if args.terminal_before is None:
        raise QueueOperationError("terminal_before_required")
    try:
        return datetime.fromisoformat(args.terminal_before)
    except ValueError:
        raise QueueOperationError("terminal_before_invalid") from None


def _cli_purge(args, _parser) -> int:
    terminal_before = _cli_terminal_before(args)
    if args.export is None:
        raise QueueOperationError("export_required")
    receipt = purge(
        terminal_before=terminal_before,
        export_path=args.export,
        include_dead=args.include_dead,
    )
    print(
        json.dumps(
            {"counts": {"purged": receipt.purged}, "ids": list(receipt.task_ids)},
            sort_keys=True,
        )
    )
    return 0


def _cli_restore(args, _parser) -> int:
    if args.export is None:
        raise QueueOperationError("export_required")
    restored = restore(export_path=args.export)
    print(
        json.dumps(
            {
                "counts": {"restored": restored.restored},
                "ids": [
                    {"exported": exported, "restored": current}
                    for exported, current in restored.task_ids
                ],
            },
            sort_keys=True,
        )
    )
    return 0


_CLI_COMMANDS = {
    "list": _cli_list,
    "status": _cli_status,
    "work": _cli_work,
    "migrate": _cli_migrate,
    "quarantine-corrupt": _cli_quarantine_corrupt,
    "purge-corrupt": _cli_purge_corrupt,
    "cancel": _cli_cancel,
    "redrive": _cli_redrive,
    "purge": _cli_purge,
    "restore": _cli_restore,
}


def _cli() -> int:
    parser = _build_cli_parser()
    try:
        args = parser.parse_args()
        handler = _CLI_COMMANDS.get(args.command)
        if handler is None:
            return 2
        return handler(args, parser)
    except _InvalidArguments:
        return _emit_invalid_arguments()
    except Exception as exc:  # noqa: BLE001 - CLI is a redacted trust boundary
        return _emit_cli_error(exc)


if __name__ == "__main__":
    raise SystemExit(_cli())
