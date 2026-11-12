"""Bounded, capability-honest Pyright language-server session.

The session lives in the concern parts `llm_wiki.pyright_session_*`;
this module keeps the import path its callers use. Code inside the product and
the tests imports a name from the part that defines it. See
`docs/research/2026-11-10-giant-files-become-concern-modules.md`.
"""

from llm_wiki.pyright_session_bootstrap import CLIENT_CAPABILITIES as CLIENT_CAPABILITIES
from llm_wiki.pyright_session_core import LanguageServerSession as LanguageServerSession
from llm_wiki.pyright_session_core import PyrightSession as PyrightSession
from llm_wiki.pyright_session_diagnostics import MAX_DIAGNOSTIC_BYTES as MAX_DIAGNOSTIC_BYTES
from llm_wiki.pyright_session_documents import MAX_DOCUMENT_BYTES as MAX_DOCUMENT_BYTES
from llm_wiki.pyright_session_documents import MAX_OPEN_DOCUMENT_BYTES as MAX_OPEN_DOCUMENT_BYTES
from llm_wiki.pyright_session_documents import MAX_OPEN_DOCUMENTS as MAX_OPEN_DOCUMENTS
from llm_wiki.pyright_session_documents import (
    verified_document_content as verified_document_content,
)
from llm_wiki.pyright_session_launch import LaunchServerGuard as LaunchServerGuard
from llm_wiki.pyright_session_manager import IDLE_SECONDS as IDLE_SECONDS
from llm_wiki.pyright_session_manager import MAX_LSP_PROCESSES as MAX_LSP_PROCESSES
from llm_wiki.pyright_session_manager import (
    LanguageServerSessionManager as LanguageServerSessionManager,
)
from llm_wiki.pyright_session_manager import PyrightSessionManager as PyrightSessionManager
from llm_wiki.pyright_session_paths import ensure_lsp_parent as ensure_lsp_parent
from llm_wiki.pyright_session_progress import (
    PROGRESS_GATE_GRACE_SECONDS as PROGRESS_GATE_GRACE_SECONDS,
)
from llm_wiki.pyright_session_progress import configuration_result as configuration_result
from llm_wiki.pyright_session_ranges import location_key as location_key
from llm_wiki.pyright_session_ranges import lsp_range as lsp_range
from llm_wiki.pyright_session_startup import HEALTHY_RUN_SECONDS as HEALTHY_RUN_SECONDS
from llm_wiki.pyright_session_startup import STARTUP_RETRY_BACKOFF as STARTUP_RETRY_BACKOFF
from llm_wiki.pyright_session_startup import STARTUP_SECONDS as STARTUP_SECONDS
from llm_wiki.pyright_session_values import LspDiagnostic as LspDiagnostic
from llm_wiki.pyright_session_values import LspLocation as LspLocation
from llm_wiki.pyright_session_values import OpenDocument as OpenDocument
from llm_wiki.pyright_session_values import ProviderCalls as ProviderCalls
from llm_wiki.pyright_session_values import ProviderDiagnostics as ProviderDiagnostics
from llm_wiki.pyright_session_values import ProviderHover as ProviderHover
from llm_wiki.pyright_session_values import ProviderLocations as ProviderLocations
