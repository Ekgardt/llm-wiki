; TypeScript symbol query, using node types shared with JavaScript.

(function_declaration
  name: (identifier) @function.name) @function.node

(method_definition
  name: (property_identifier) @function.name) @function.node

(class_declaration
  name: (type_identifier) @class.name) @class.node

(interface_declaration
  name: (type_identifier) @class.name) @class.node

(call_expression
  function: (identifier) @call.name) @call.node

(call_expression
  function: (member_expression
    property: (property_identifier) @call.name)) @call.node

(import_statement
  source: (string) @import.name) @import.node

(variable_declarator
  name: (identifier) @function.name
  value: (arrow_function)) @function.node
