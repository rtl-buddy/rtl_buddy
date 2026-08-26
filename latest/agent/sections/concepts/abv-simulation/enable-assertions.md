## Enable assertions

```yaml
tests:
  - name: smoke_with_sva
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    assertions: true
```

For Verilator, rtl_buddy adds `--assert` and `--coverage-user` unless the builder options already contain them. For other builders, the setting has no effect and logs `compile.assertions_not_verilator` at WARNING.

Verilator supports immediate assertions, common synchronous concurrent assertions, cover properties, and some sequence operators. It does not implement the full IEEE 1800 assertion language. Check the [Verilator language support](https://verilator.org/guide/latest/languages.html) for the installed version before relying on constructs such as `disable iff`, local property variables, or advanced sequence operators. Use [`rb fpv`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/fpv/) with the slang frontend or a simulator with the required SVA support when Verilator cannot compile the property set.
