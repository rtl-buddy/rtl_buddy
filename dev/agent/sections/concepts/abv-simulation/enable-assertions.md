## Enable assertions

```yaml
tests:
  - name: smoke_with_sva
    model: my_design
    model_path: ../src/models.yaml
    testbench: tb_top
    assertions: true
```

For Verilator, rtl_buddy adds `--assert` and `--coverage-user` unless the builder options already include them. For other builders the setting has no effect, and rtl_buddy logs `compile.assertions_not_verilator` at WARNING.

Verilator supports immediate assertions, common synchronous concurrent assertions, cover properties, and some sequence operators, but not the full IEEE 1800 assertion language. Check the [Verilator language support](https://verilator.org/guide/latest/languages.html) for your version before using `disable iff`, local property variables, or advanced sequence operators. If Verilator cannot compile the properties, use [`rb fpv`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpv/) with the slang frontend, or a simulator with the needed SVA support.
