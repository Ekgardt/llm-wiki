; Original Go extraction query built from the grammar's node-types.json.

(function_declaration
  name: (identifier) @function.name) @function.node

(method_declaration
  name: (field_identifier) @function.name) @function.node

(type_declaration
  (type_spec
    name: (type_identifier) @class.name
    type: [(struct_type) (interface_type)])) @class.node

(call_expression
  function: (identifier) @call.name) @call.node

(call_expression
  function: (selector_expression
    field: (field_identifier) @call.name)) @call.node

(import_spec
  path: (interpreted_string_literal) @import.name) @import.node

(import_spec
  path: (raw_string_literal) @import.name) @import.node
