# Strict UTF-8 prefixes

`llm_wiki.utf8_text._utf8_prefix` is the existing renderer helper, moved with its
body/signature unchanged. It encodes strict UTF-8, retains fitting text and clips
an oversized encoded prefix only at a complete scalar boundary. Original invalid
Unicode raises even if a small ceiling could otherwise hide it. It does not
normalize graphemes, replace malformed input or choose a new size limit.

The renderer keeps an alias and the helper retains its serialization module.
There is one implementation and no consumer import. Pure boundary cases first
ran against the renderer; original renderer tests remain unchanged. Public alias
and golden-pickle qualification live in tests/test_worker_text_compatibility.py,
outside this pure packet, with the original golden bytes retained here.

Callers own input policy and byte ceilings. The planned queue correction may
prepare exception-type text before calling this strict primitive; that must not
weaken renderer validation. No I/O, runtime imports or dependencies are added.
Run `pytest tests/modules/utf8_text tests/test_worker_text_compatibility.py
 tests/test_code_navigation_renderer.py -q`.
The packet retains all exact source/tests, bootstrap, instructions, research and
measurement code. A prefix transfer alone does not fix queue failure settlement.
