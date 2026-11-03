"""Lease time policy exercised against the original implementation before transfer."""
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from llm_wiki import ownership_timing as timing


@pytest.mark.parametrize('offset', [-720, -330, 0, 345, 840])
def test_offsets_preserve_the_instant(offset):
    value = datetime(2026, 9, 17, 12, 0, 0, 123456,
                     tzinfo=timezone(timedelta(minutes=offset)))
    written = timing._timestamp(value)
    assert written.endswith('123456Z')
    assert timing._parse_timestamp(written) == value
    assert timing._as_utc(value).tzinfo is UTC


def test_text_order_matches_time_across_whole_seconds():
    whole = datetime(2026, 9, 17, 12, tzinfo=UTC)
    values = [whole + timedelta(microseconds=n) for n in
              (-1000001, -1, 0, 1, 500000, 999999, 1000000)]
    written = [timing._timestamp(value) for value in values]
    assert written == sorted(written)
    assert len(set(written)) == len(values)
    assert [timing._parse_timestamp(value) for value in written] == values


@pytest.mark.parametrize('value', [None, '2026-09-17', datetime(2026, 9, 17)])
def test_normalization_refuses_unzoned_or_non_datetime_values(value):
    with pytest.raises(ValueError, match='timezone-aware datetime'):
        timing._as_utc(value)


@pytest.mark.parametrize('value', [None, 123, '', 'not a timestamp'])
def test_invalid_record_preserves_error_code(value):
    with pytest.raises(timing.OperationalOwnershipError) as error:
        timing._parse_timestamp(value)
    assert error.value.code == 'owner_record_invalid'


def test_parseable_but_unzoned_record_is_not_assumed_utc():
    with pytest.raises(ValueError, match='timezone-aware datetime'):
        timing._parse_timestamp('2026-09-17T12:00:00')


@pytest.mark.parametrize('role,expected', [
    ('capture', (30, 10)), ('project', (30, 10)),
    ('markdown-writer', (30, 10)), ('queue-worker', (120, 40)),
    ('compile', (120, 40)), ('doctor', (30, 10)), ('nightly', (120, 40)),
    ('weekly', (120, 40)), ('lsp', (30, 10)), ('queue-operator', (120, 40)),
    ('repair', (120, 40)), ('runtime-deletion-check', (30, 10)),
])
def test_existing_role_durations_and_supplied_lease_refusal(role, expected):
    assert timing._timing(role) == expected
    lease = SimpleNamespace(ttl_seconds=expected[0], heartbeat_seconds=expected[1])
    timing._require_lease_timing(lease, role)
    lease.ttl_seconds += 1
    with pytest.raises(ValueError, match='lease timing does not match its role'):
        timing._require_lease_timing(lease, role)
    lease.ttl_seconds = expected[0]
    lease.heartbeat_seconds += 1
    with pytest.raises(ValueError, match='lease timing does not match its role'):
        timing._require_lease_timing(lease, role)
