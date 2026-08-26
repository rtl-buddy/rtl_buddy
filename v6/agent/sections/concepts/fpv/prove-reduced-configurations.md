## Prove reduced configurations

Use `params` to reduce a width or depth when the full state space is impractical:

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

Names must be identifiers. Values may be integers, booleans, or strings containing verbatim SystemVerilog literal text. String-valued parameters need embedded quotes, for example `MODE: '"small"'`. Whitespace in values is rejected, as are YAML 1.1 boolean-like keys such as unquoted `on` or `off`.

The verilog frontend applies overrides with `chparam`; slang applies them during `read_slang` with `-G`. The same values apply to the primary proof, vacuity, and COI passes.

A reduced proof establishes only that configuration. Keep a full-size run at a feasible depth when the shipping configuration also needs coverage.
