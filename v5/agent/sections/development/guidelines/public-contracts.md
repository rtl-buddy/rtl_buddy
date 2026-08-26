## Public Contracts

Treat CLI behavior, YAML config loading, generated artifact layout, machine-mode output, event names, and bundled skill behavior as public interfaces.
Downstream RTL projects and automation depend on them.

Prefer targeted changes over broad refactors.
When a change intentionally alters a contract, update docs, tests, generated references, and downstream validation assets in the same PR.
