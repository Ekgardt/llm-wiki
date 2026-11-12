"""Helpers shared by test_daily_entry_range_boundaries.py and the test files importing them."""

import re

import evidence_resolver as resolver


def _previous_starts(content):
    headings = re.finditer(rb"(?m)^## \[([^\]\r\n]+)\][^\r\n]*(?:\r?\n|$)", content)
    result = [(match.start(), _previous_heading_id(match[1])) for match in headings]
    markers = re.finditer(rb"(?m)^<!-- llm-wiki-operation:[0-9a-f]+ -->[^\r\n]*(?:\r?\n|$)", content)
    return sorted(result + [(match.start(), None) for match in markers])


def _previous_heading_id(raw):
    try:
        block_id = raw.decode('utf-8', errors='strict')
    except UnicodeDecodeError as error:
        raise resolver.EvidenceResolutionError('daily block ID is not UTF-8') from error
    if resolver.BLOCK_ID_RE.fullmatch(block_id) is None:
        raise resolver.EvidenceResolutionError('daily block ID is invalid')
    return block_id


def _outcome(parser, content):
    try:
        return parser(content)
    except resolver.EvidenceResolutionError as error:
        return type(error), str(error)


_HEADERS = (
    b'## [id]', b'## [12:34:56] Heading', b'## [id] suffix\rbroken',
    b'## []', b'## [missing', b'## [bad id]', b'## [\xff]',
    b'## [id] ] more', b'## [a[b]', b'## [id\nsplit]',
    b'<!-- llm-wiki-operation:abc -->',
    b'<!-- llm-wiki-operation:0 -->tail',
    b'<!-- llm-wiki-operation: -->',
    b'<!-- llm-wiki-operation:ABC -->',
    b'<!-- llm-wiki-operation:abc  -->',
    b'<!-- llm-wiki-operation:abc -->tail\rbroken',
    b'<!-- llm-wiki-operation:abc --> -->',
)
