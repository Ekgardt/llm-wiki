; Original C++ extraction query built from the grammar's node-types.json.

(function_definition
  declarator: (function_declarator
    declarator: [(identifier) (field_identifier)] @function.name)) @function.node

[(class_specifier) (struct_specifier)] @class.node

[(class_specifier) (struct_specifier)
  name: (type_identifier) @class.name]

(call_expression
  function: (identifier) @call.name) @call.node

(call_expression
  function: (field_expression
    field: (field_identifier) @call.name)) @call.node

(call_expression
  function: (qualified_identifier
    name: (identifier) @call.name)) @call.node

(preproc_include
  path: [(string_literal) (system_lib_string)] @import.name) @import.node
