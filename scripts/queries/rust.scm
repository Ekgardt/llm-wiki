; Original Rust extraction query built from the grammar's node-types.json.

(function_item
  name: (identifier) @function.name) @function.node

[(struct_item) (enum_item) (union_item) (trait_item)] @class.node

[(struct_item) (enum_item) (union_item) (trait_item)
  name: (type_identifier) @class.name]

(call_expression
  function: (identifier) @call.name) @call.node

(call_expression
  function: (field_expression
    field: (field_identifier) @call.name)) @call.node

(macro_invocation
  macro: (identifier) @call.name) @call.node

(use_declaration
  argument: (_) @import.name) @import.node

(mod_item
  name: (identifier) @import.name) @import.node
