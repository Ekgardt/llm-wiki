"""Original complete-map and identity checks, without collector imports."""
import hashlib
import json

from code_languages import CODE_LANGUAGE_BY_SUFFIX

from tests.modules.code_languages.expected_languages import EXPECTED_CODE_LANGUAGE_BY_SUFFIX


def test_code_language_map_exactly_matches_independent_complete_contract():
    assert dict(CODE_LANGUAGE_BY_SUFFIX) == EXPECTED_CODE_LANGUAGE_BY_SUFFIX


def test_code_language_classifier_identity_binds_version_and_canonical_map():
    from code_languages import (
        CLASSIFIER_IDENTITY,
        CLASSIFIER_MAP_SHA256,
        CLASSIFIER_VERSION,
    )

    canonical = json.dumps(
        EXPECTED_CODE_LANGUAGE_BY_SUFFIX,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    expected_digest = hashlib.sha256(canonical).hexdigest()

    assert CLASSIFIER_VERSION == "code-language-classifier/v1"
    assert CLASSIFIER_MAP_SHA256 == expected_digest
    assert CLASSIFIER_IDENTITY == f"{CLASSIFIER_VERSION}+sha256:{expected_digest}"
