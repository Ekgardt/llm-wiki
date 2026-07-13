; Original PHP extraction query built from the grammar's node-types.json.

(function_definition
  name: (name) @function.name) @function.node

(method_declaration
  name: (name) @function.name) @function.node

[(class_declaration) (interface_declaration) (trait_declaration)] @class.node

[(class_declaration) (interface_declaration) (trait_declaration)
  name: (name) @class.name]

(function_call_expression
  function: [(qualified_name) (name)] @call.name) @call.node

(member_call_expression
  name: (name) @call.name) @call.node

(scoped_call_expression
  name: (name) @call.name) @call.node

[(include_expression [(string (string_content) @import.name)
                      (encapsed_string (string_content) @import.name)])
 (include_once_expression [(string (string_content) @import.name)
                           (encapsed_string (string_content) @import.name)])
 (require_expression [(string (string_content) @import.name)
                      (encapsed_string (string_content) @import.name)])
 (require_once_expression [(string (string_content) @import.name)
                           (encapsed_string (string_content) @import.name)])] @import.node

(namespace_use_declaration
  (namespace_use_clause
    (qualified_name) @import.name)) @import.node
