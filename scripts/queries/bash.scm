; Original Bash extraction query built from the grammar's node-types.json.

(function_definition
  name: (word) @function.name) @function.node

(command
  name: (command_name
    (word) @call.name)) @call.node

((command
  name: (command_name
    (word) @import.command)
  argument: (word) @import.name) @import.node
  (#any-of? @import.command "source" "."))

; Bash has no class declaration syntax. Compound statements stay attached
; to their function_definition capture through @function.node.

(command_substitution
  (command
    name: (command_name
      (word) @call.name))) @call.node
