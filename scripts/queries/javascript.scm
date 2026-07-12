; tree-sitter query for JavaScript — symbol extraction

(function_declaration name: (identifier) @function.name) @function.def
(class_declaration name: (identifier) @class.name) @class.def
(method_definition name: (property_identifier) @method.name) @method.def
(call_expression function: (identifier) @call.name) @call
(import_statement (import_clause (identifier) @import.name)) @import
(lexical_declaration (variable_declarator name: (identifier) @var.name) value: (arrow_function)) @arrow.func
