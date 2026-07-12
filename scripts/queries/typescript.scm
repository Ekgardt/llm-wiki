; tree-sitter query for TypeScript — symbol extraction
; Extends JavaScript with type annotations and interfaces.

(function_declaration name: (identifier) @function.name) @function.def
(class_declaration name: (type_identifier) @class.name) @class.def
(method_definition name: (property_identifier) @method.name) @method.def
(call_expression function: (identifier) @call.name) @call
(interface_declaration name: (type_identifier) @interface.name) @interface.def
(type_alias_declaration name: (type_identifier) @type.name) @type.def
(import_statement (import_clause (identifier) @import.name)) @import
