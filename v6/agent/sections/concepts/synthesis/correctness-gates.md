## Correctness gates

Three gates check the Yosys elaboration stage of both backends for netlists that are wrong without any error. Set them in `cfg-synth-tools.opts`:

```yaml
opts:
  static-functions: error        # error | warn | allow
  conflicting-drivers: error     # error | allow
  unresolved-interfaces: warn    # error | warn | allow
```

An unrecognized value is fatal.
