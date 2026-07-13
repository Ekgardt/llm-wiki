; Original Ruby extraction query built from the grammar's node-types.json.

(method
  name: (_) @function.name) @function.node

(singleton_method
  name: (_) @function.name) @function.node

[(class) (module)
  name: [(constant) (scope_resolution)] @class.name] @class.node

(call
  method: (identifier) @call.name) @call.node

((call
  method: (identifier) @import.command
  arguments: (argument_list
    (string
      (string_content) @import.name))) @import.node
  (#any-of? @import.command "require" "load"))

(alias
  name: (_) @function.name) @function.node
