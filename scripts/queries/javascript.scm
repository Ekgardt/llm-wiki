; Original JavaScript extraction query built from the grammar's node-types.json.

(function_declaration
  name: (identifier) @function.name) @function.node

(method_definition
  name: (property_identifier) @function.name) @function.node

(class_declaration
  name: (identifier) @class.name) @class.node

(call_expression
  function: (identifier) @call.name) @call.node

(call_expression
  function: (member_expression
    property: (property_identifier) @call.name)) @call.node

(import_statement
  source: (string) @import.name) @import.node

(variable_declarator
  name: (identifier) @function.name
  value: [(arrow_function) (function_expression)]) @function.node
