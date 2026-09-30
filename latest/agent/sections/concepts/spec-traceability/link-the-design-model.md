## Link the design model

Point the model at `specs.yaml` with a path relative to `models.yaml`:

```yaml
models:
  - name: my_block
    filelist: [-F my_block.f]
    spec: ../../spec/my_block/specs.yaml
```

With a multi-block spec, the model name selects the block of the same name. A single-block spec matches unconditionally.
