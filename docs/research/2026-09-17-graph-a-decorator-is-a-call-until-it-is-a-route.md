# A decorator is a call until it is a route, and a route names its methods

Date: 2026-09-17. Audit 3, code intelligence, finding B25 (three defects of one
family in the Python extractor).

Files: `scripts/code_extractor.py`, `scripts/code_hints.py`,
`tests/test_a_decorator_is_a_call_until_it_is_a_route.py`

## What was found (each reproduced)

1. **Every decorator that is a call loses its `CALLS` edge.**
   `CodeExtractor._is_route_decorator` answered "is this `ast.Call` directly in
   some `FunctionDef.decorator_list`" and nothing more, and
   `_python_node_edges` skips `_call_edges` for anything it says yes to. So
   `@factory(1)`, `@pytest.mark.parametrize(...)`, `@functools.lru_cache(maxsize=8)`
   and every other parameterised decorator on a function produce no `CALLS`
   edge and no observation either — they vanish. A decorator on a *class* is not
   checked at all and keeps its edge, so the same source line means two
   different things depending on what it decorates.

2. **`@app.route` is filed under a method that no client can ask for.**
   `_route` computes `method = function.attr.upper()`, which for Flask's
   `@app.route("/users")` is the literal string `ROUTE`. The route node is
   stored under `("ROUTE", "/users")`, while the HTTP-client side of the same
   extractor looks a path up under `("GET", "/users")` and friends
   (`_HTTP_METHODS` is `get/post/put/patch/delete`). A Flask service therefore
   never links a `requests.get("/users")` call to the view that serves it.
   `code_hints.find_routes` reads the same `method` column and shows `ROUTE` to
   the operator.

3. **One deep expression aborts the whole extraction.** `ast.unparse` is
   recursive. `a.b.b.b…()` 500 deep parses without complaint — an attribute
   chain is not nested source — and then raises `RecursionError` out of
   `ast.unparse(node.func)` in `_call_edges`, past `_parsed_python`'s
   `except (SyntaxError, ValueError, UnicodeError)`, past `collect_python_edges`
   and out of `extract_code`. One file ends the generation. The same
   `ast.unparse` is reached from the `INHERITS` edge, the decorator observation,
   the signature's annotations and the argument-binding text.

## Sources

- Flask API documentation,
  [`Flask.add_url_rule` / `Flask.route`](https://flask.palletsprojects.com/en/stable/api/)
  (fetched 2026-09-17): "The `methods` parameter defaults to `[\"GET\"]`. `HEAD`
  is always added automatically, and `OPTIONS` is added automatically by
  default." So `@app.route("/x")` is a GET route, and
  `@app.route("/x", methods=["POST"])` is a POST route — the verb is in the
  `methods` keyword, never in the attribute name.
- Python documentation, `ast` →
  [`ast.unparse`](https://docs.python.org/3/library/ast.html#ast.unparse)
  (fetched 2026-09-17): "Unparse an ast.AST object and generate a string with
  code that would produce an equivalent ast.AST object if parsed back with
  ast.parse()." — followed on the same page by the warning "Trying to unparse a
  highly complex expression would result with `RecursionError`."
- In-repository precedent for bounding a recursive walk rather than trusting
  the interpreter's stack: `scripts/lsp_protocol.py`'s "iterative depth/count/
  byte bounds" over incoming JSON, which the same audit checked and found clean.

## Decision

1. A call is a route decorator exactly when a route node was minted for it.
   `_route` records `id(decorator)` when it creates the route, and
   `_is_route_decorator` reads that set. The parent map is no longer consulted,
   which also removes the function/class asymmetry: every decorator that is not
   a route keeps its `CALLS` edge, whatever it decorates. This is exact rather
   than approximate, because definitions are collected before edges within one
   `extract()` and the trees stay alive for the whole of it.
2. `route` is translated to the verbs it declares: the string constants of the
   `methods=` keyword, upper-cased, or `("GET",)` per the Flask documentation
   above when the decorator names no `methods=` at all, names it with something
   that is not a literal list, tuple or set, or fills it with values that are
   not string constants. In that last case the extractor cannot read what the
   source says, and falls back to the framework's own default rather than
   inventing a verb. One decorator may therefore mint more than one
   route node — one per verb — which is what the source says. `HEAD` and
   `OPTIONS` are not minted: they are the framework's addition, not the
   repository's declaration, and nothing asks for them. Verb decorators
   (`@app.get`, `@router.post`) are unchanged.
3. Every `ast.unparse` of a caller-supplied expression goes through
   `_expression_text`, which first measures the expression's depth with an
   iterative walk and, past `MAX_EXPRESSION_DEPTH`, returns a bounded stand-in
   instead of recursing. The extraction continues and the answer says the text
   was too deep to render, rather than the generation ending.
