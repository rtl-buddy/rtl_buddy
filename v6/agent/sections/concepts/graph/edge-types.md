## Edge Types

Design edges are `instantiates`, `child_of`, `instance_of`, `connects`, `implements`, and `overrides`.

Important cross-tier and config edges are:

| Edge | Relationship |
| --- | --- |
| `declares` | Suite to test/testbench, or spec block to coverage item. |
| `runs_on` | Test to testbench. |
| `exercises` | Testbench or non-simulation run to model. |
| `covers` | Test or formal run to coverage item. |
| `specified_by`, `documented_by`, `implements` | Model, spec block, document, and golden-model traceability. |
| `maps_to` | Model declaration to design module. |
| `elaborates_as` | Testbench to its elaborated top module. |
| `targets` | Non-simulation run to its top module. |

Binding edges are `binds_to`, `imports`, `drives`, `checks_against`, and `implemented_by`. Only `drives` and `implemented_by` may be `INFERRED`; unresolved signal or symbol matches carry `resolved: false`. A `via` field identifies evidence inherited through a helper module.

The config tier reads the same loaders as the test, spec, and regression commands.
