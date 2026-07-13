; Original C extraction query built from the grammar's node-types.json.

(function_definition
  declarator: (function_declarator
    declarator: (identifier) @function.name)) @function.node

(struct_specifier
  name: (type_identifier) @class.name
  body: (field_declaration_list)) @class.node

(union_specifier
  name: (type_identifier) @class.name
  body: (field_declaration_list)) @class.node

(call_expression
  function: (identifier) @call.name) @call.node

(call_expression
  function: (field_expression
    field: (field_identifier) @call.name)) @call.node

(preproc_include
  path: [(string_literal) (system_lib_string)] @import.name) @import.node
