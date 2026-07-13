; Original C# extraction query built from the grammar's node-types.json.

(method_declaration
  name: (identifier) @function.name) @function.node

(constructor_declaration
  name: (identifier) @function.name) @function.node

[(class_declaration) (interface_declaration) (struct_declaration)] @class.node

[(class_declaration) (interface_declaration) (struct_declaration)
  name: (identifier) @class.name]

(invocation_expression
  function: (identifier) @call.name) @call.node

(invocation_expression
  function: (member_access_expression
    name: (identifier) @call.name)) @call.node

(object_creation_expression
  type: (identifier) @call.name) @call.node

(using_directive
  [(identifier) (qualified_name)] @import.name) @import.node
