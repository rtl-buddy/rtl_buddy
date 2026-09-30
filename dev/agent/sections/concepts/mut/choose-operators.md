## Choose operators

| Operator | Mutation |
|---|---|
| `arith_flip` | Flip an arithmetic operator |
| `bit_op_flip` | Flip a bitwise or logical operator |
| `cond_negate` | Negate a condition |
| `cond_const` | Replace a condition with a constant |
| `assign_drop` | Drop an assignment |
| `port_binding_swap` | Swap two port bindings |

An empty operator list, or an operator the installed engine does not support, is fatal.
