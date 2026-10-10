"""Test reporting must not duplicate the evidence being verified."""
import hashlib
import importlib

import pytest


@pytest.mark.parametrize("module,name,digests", [
    ("test_breadcrumb_protocol",
     "test_every_byte_is_restored_and_every_record_fits_the_existing_transport", (
         "81db8ebbbbc69c6c6ad4a6aa92b76e0c08af547da236b9e2c9dbe1d8285a8130",
         "c37e3f67d74c538203c29cd44e239c32a0795a73d0a8430c4276ed8710aa7e5d",
         "8f990ba0b577b51cf009ea049368c16bbda1b21e1b93be07a824758bb253c39b",
         "38e7096ec5e58df2b4275fdb3e2b11d8fdbe329549e883f9464bce55201b4264",
     )),
    ("test_breadcrumb_evidence",
     "test_permanent_markdown_reconstructs_the_complete_source_without_runtime", (
         "0c1b31f9e677de6de49b5995dba7fabebc4f7e9a87181a5494102c1ee76b76be",
         "58747b68e92afd2029fa4ba88d06d5390c15c6da877c6f3d0a81c7bd6c61bbfe",
         "6932fd31e5daf4739b9fa78ff777b2831b0995cc1d0b0093cac80601902013bc",
     )),
], ids=["transport-evidence", "permanent-evidence"])
def test_payloads_remain_complete_but_report_names_are_explicit(module, name, digests):
    function = getattr(importlib.import_module("tests." + module), name)
    marker = function.pytestmark[0]
    payloads = [_bytes(value) for value in marker.args[1]]

    assert tuple(hashlib.sha256(value).hexdigest() for value in payloads) == digests
    identifiers = marker.kwargs.get("ids")
    assert identifiers is not None, "Default test IDs copy the complete payload into reports"
    assert len(identifiers) == len(payloads)
    assert len(set(identifiers)) == len(identifiers)
    assert all(identifier.isascii() for identifier in identifiers)
    assert all(identifier.encode() not in payloads for identifier in identifiers)


def _bytes(value):
    if isinstance(value, str):
        return value.encode("utf-8")
    return value
