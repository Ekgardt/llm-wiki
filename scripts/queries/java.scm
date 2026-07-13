; Original Java extraction query built from the grammar's node-types.json.

(method_declaration
  name: (identifier) @function.name) @function.node

(constructor_declaration
  name: (identifier) @function.name) @function.node

[(class_declaration) (interface_declaration) (enum_declaration)] @class.node

[(class_declaration) (interface_declaration) (enum_declaration)
  name: (identifier) @class.name]

(method_invocation
  name: (identifier) @call.name) @call.node

(object_creation_expression
  type: (type_identifier) @call.name) @call.node

(import_declaration
  [(identifier) (scoped_identifier)] @import.name) @import.node
