# Graph storage-value adaptation

`graph_storable` is the existing standard-library-only shared policy used by
code/knowledge extractors and the evidence writer. Production remains unchanged.
Text admission requires a nonempty string within the caller's character bound,
without NUL/CR/LF. It is not a general Unicode or JSON validator.

Valid identity keys remain verbatim; others use the existing sha256 form with
UTF-8 surrogatepass. Node identity derivation stays with callers. The existing
4096-character threshold has a documented unmeasured historical basis; this
qualification neither changes it nor invents evidence for it.

Metadata recursively converts mappings and sequences, dates to ISO text, finite
floats to text, nonfinite floats to None and unencodable string surrogates to '?'.
Unsupported scalars remain unchanged and must still pass the strict writer.
Mapping keys retain the existing str conversion. This is not a universal JSON
sanitizer and introduces no new coercion or validation promise.

No database, extractor or runtime dependency may enter the owner. Current package
and top-level imports remain. Direct value tests complement the untouched real
extractor→writer regression, including long signatures and raw YAML values.
Run `pytest tests/modules/graph_storable
 tests/test_the_rest_of_the_readers_before_the_writer.py -q`.
The full packet includes exact source/tests, instructions, research, bootstrap and
lossless measurement code. No source schema, authoritative data or runtime moves.
