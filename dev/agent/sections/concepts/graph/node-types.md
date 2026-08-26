## Node Types

| Type | Id form |
| --- | --- |
| `module` | `module:<name>` |
| `instance` | `inst:<top>/<dot.path>` |
| `port` | `port:<module>.<port>` |
| `parameter` | `param:<module>.<name>` |
| `interface`, `modport` | `iface:<name>`, `modport:<interface>.<name>` |
| `suite` | `suite:<suite dir>` |
| `test` | `test:<suite dir>#<name>` |
| `testbench` | `tb:<suite dir>#<name>` |
| `model` | `model:<models.yaml>#<name>` |
| `spec_block` | `spec:<block>` |
| `coverage_item` | `covitem:<block>#<id>` |
| `spec_doc`, `golden_model` | `doc:<path>`, `golden:<path>` |
| `python_module` | Normally `py:<path>`; an extractor may supply another id for the same file. |

`reglvl` is stored as written because resolving builder-specific values requires runtime context.
