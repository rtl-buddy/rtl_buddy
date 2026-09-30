## Prove reduced configurations

When the full state space is impractical, use `params` to shrink a width or depth:

```yaml
verifications:
  - name: my_block_proof_k8
    tool: sby
    model: my_block
    top: my_block
    params:
      K: 8
    mode: bmc
    depth: 24
```

- Names must be identifiers. Values are integers, booleans, or strings holding verbatim SystemVerilog text, so a string parameter needs embedded quotes: `MODE: '"small"'`.
- Whitespace in values is rejected, as are YAML 1.1 boolean-like keys such as unquoted `on` or `off`.

A reduced proof covers only that configuration. Keep a full-size run at a feasible depth if the shipping configuration also needs it.
