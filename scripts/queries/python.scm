; tree-sitter query for Python — symbol extraction
; Used by code_graph.py to identify functions, classes, calls, imports.

(function_definition name: (identifier) @function.name) @function.def
(class_definition name: (identifier) @class.name) @class.def
(call function: (identifier) @call.name) @call
(call function: (attribute attribute: (identifier) @call.method)) @call.attr
(import_statement (dotted_name (identifier) @import.name)) @import
(import_from_statement (dotted_name (identifier) @import.from)) @import.from
