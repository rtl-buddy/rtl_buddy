## Workflow

### 1. Write the spec

Create `spec/<block>/specs.yaml` with one or more blocks, each listing its functional coverage items:

```yaml
rtl-buddy-filetype: spec_config

blocks:
  - name: "my_block"
    desc: "Brief description"
    docs:
      - "README.md"
      - "behavior.md"
    coverage-items:
      - id: "MYBLK-COV-01"
        desc: "Normal operation path verified"
      - id: "MYBLK-COV-02"
        desc: "Error handling and recovery"
```

Coverage item IDs are arbitrary strings; a block-prefix convention (e.g. `MYBLK-COV-NN`) keeps them unique across a multi-block project.

### 2. Link the design model

Add a `spec:` field to the relevant entry in `design/<block>/models.yaml`, pointing to the `specs.yaml` relative to that `models.yaml`:

```yaml
models:
  - name: "my_block"
    filelist:
      - "-F my_block.f"
    spec: "../../spec/my_block/specs.yaml"
```

The model `name` is matched against the block `name` in the referenced `specs.yaml`. In a single-block `specs.yaml` the match is unconditional.

### 3. Annotate tests

Add `covers:` to each test in `verif/<block>/tests.yaml` listing the IDs it exercises:

```yaml
tests:
  - name: "basic"
    model: "my_block"
    model_path: "../../design/my_block/models.yaml"
    testbench: "tb_top"
    covers:
      - "MYBLK-COV-01"
      - "MYBLK-COV-02"
```

A single test can cover multiple items; multiple tests can cover the same item.

### 4. Check traceability

```bash
rb spec list             # discover all spec blocks in the project
rb spec check-design     # which spec blocks have a linked design model
rb spec check-coverage   # which coverage items are addressed by at least one test
```

`check-coverage` prints a table of all items with a `Covered: yes/no` column and calls out uncovered IDs at the end. Use `--machine` to get JSON output for programmatic consumption.
