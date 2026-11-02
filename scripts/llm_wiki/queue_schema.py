"""Existing queue v3 SQL definitions; execution and migrations belong to memory_queue."""

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

