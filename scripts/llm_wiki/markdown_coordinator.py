"""The Markdown coordinator, composed from its concern mixins."""
from __future__ import annotations

from llm_wiki.markdown_abort import TransactionAbort
from llm_wiki.markdown_apply import TransactionApplication
from llm_wiki.markdown_checkpoints import ProjectCheckpointOperations
from llm_wiki.markdown_coordinator_base import MarkdownCoordinatorFoundation
from llm_wiki.markdown_operations import OperationExecution
from llm_wiki.markdown_preconditions import PreconditionChecks
from llm_wiki.markdown_prepare import TransactionPreparation
from llm_wiki.markdown_records_access import TransactionRecordOperations
from llm_wiki.markdown_recovery import TransactionRecovery
from llm_wiki.markdown_targets import TargetCaptureOperations
from llm_wiki.markdown_undo import TransactionUndoAndPrune
from llm_wiki.markdown_writer_gate import WriterGateOperations


class MarkdownCoordinator(MarkdownCoordinatorFoundation, ProjectCheckpointOperations, TransactionRecordOperations, TransactionPreparation, TransactionApplication, TransactionAbort, TransactionRecovery, TransactionUndoAndPrune, WriterGateOperations, TargetCaptureOperations, PreconditionChecks, OperationExecution):
    """Prepare and apply durable multi-file Markdown transactions."""
