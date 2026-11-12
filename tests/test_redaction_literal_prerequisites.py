import secret_redact as redactor

from tests.modules.secret_redact.pattern_prerequisites_helpers import original_patterns


def full_packing(inputs):
    from llm_client import planning_input_text
    from llm_wiki import compile_pipeline_base

    from tests.compile_reuses_raw_row_positions_within_one_layout_helpers import _packing_result

    result = _packing_result(inputs)
    wires = tuple(planning_input_text(prompt, compile_pipeline_base.DRAFT_SYSTEM, schema)
                  for prompt, schema in result[1])
    return result, wires


def rss_high_water():
    try:
        import resource
    except ImportError:
        return None
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def measure_packing(inputs):
    import time

    wall, cpu = time.monotonic(), time.process_time()
    result = full_packing(inputs)
    return result, time.monotonic() - wall, time.process_time() - cpu, rss_high_water()


def test_matched_complete_packing_layout_bindings_and_wire(tmp_path, monkeypatch, record_property):
    from dataclasses import replace

    from llm_wiki import compile_pipeline_sources

    from tests.compile_binds_the_protected_source_view_helpers import _legacy_packet
    from tests.compile_reuses_raw_row_positions_within_one_layout_helpers import _context_pages

    inputs = _legacy_packet(tmp_path, monkeypatch, [f'Факт {index} is settled.' for index in range(750)])
    parts = tuple(compile_pipeline_sources.daily_parts(inputs.dailies[0].logical_path, inputs.dailies[0].content))
    inputs = replace(inputs, dailies=parts, sources=(*inputs.sources, *_context_pages()))
    current = redactor.redact_patterns
    results = []
    for name, function in [('original', original_patterns), ('candidate', current),
                           ('candidate', current), ('original', original_patterns)]:
        monkeypatch.setattr(redactor, 'redact_patterns', function)
        result, wall, cpu, rss = measure_packing(inputs)
        results.append(result)
        record_property(name + '_wall', wall)
        record_property(name + '_cpu', cpu)
        record_property(name + '_rss_high_water_kib', rss)
    assert all(result == results[0] for result in results)
    assert results[0][0][0]
