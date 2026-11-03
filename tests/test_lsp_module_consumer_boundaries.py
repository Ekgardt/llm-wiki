"""Consumer integration assertions retained from leaf module qualification."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def test_lsp_credentials_security_has_no_duplicate_credential_implementation():
    tree = ast.parse((ROOT / 'scripts/lsp_security.py').read_text())
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert not names.intersection({'_quoted_value_end', '_line_value_end', '_assignment_value_end', '_redact_assignments', '_standalone_replacement', '_redact_standalone_secrets', '_scheme_character', '_scheme_start', '_is_scheme', '_authority_terminator', '_authority_end', '_userinfo_span', '_redact_url_userinfo', 'redact_private_key_blocks'})


def test_lsp_text_security_has_no_second_normalization_implementation():
    tree = ast.parse((ROOT / 'scripts/lsp_security.py').read_text())
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not defined.intersection({'_is_control', '_collapses_to_space', '_LogScanner', '_normalize_log_text'})


def test_lsp_wire_transport_does_not_define_a_second_codec():
    tree = ast.parse((ROOT / 'scripts/lsp_protocol.py').read_text())
    defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    assert not defined.intersection({'encode_frame', 'json_depth', 'JsonRpcFrameReader', 'ProtocolViolation', 'FrameTooLarge'})
