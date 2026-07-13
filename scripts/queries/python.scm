; Original Python extraction query built from the grammar's node-types.json.

(function_definition
  name: (identifier) @function.name) @function.node

(class_definition
  name: (identifier) @class.name) @class.node

(call
  function: (identifier) @call.name) @call.node

(call
  function: (attribute
    attribute: (identifier) @call.name)) @call.node

(import_statement
  name: (dotted_name) @import.name) @import.node

(import_from_statement
  module_name: (dotted_name) @import.name) @import.node
