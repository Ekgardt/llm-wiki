"""A full repair scenario remains complete after a slower unrelated probe."""
import time

import tests.test_the_json_queue_import_goes as scenario


class _ProbeClock:
    def __init__(self):
        self.offset = 0.0

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        return time.monotonic() + self.offset


def test_legacy_queue_repair_completes_after_a_probe_outlasting_quick_health(tmp_path, monkeypatch):
    clock = _ProbeClock()
    original = scenario.doctor._filesystem_check

    def slower_probe(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.offset += 2 * scenario.doctor.DEFAULT_TIME_BUDGET_SECONDS
        return result

    monkeypatch.setattr(scenario.doctor, "time", clock)
    monkeypatch.setattr(scenario.doctor, "_filesystem_check", slower_probe)
    scenario.test_doctor_repair_neither_imports_nor_marks_the_json_queue(tmp_path, monkeypatch)
