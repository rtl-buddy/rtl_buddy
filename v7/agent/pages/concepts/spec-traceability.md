---
description: Link specification items to design models, simulation tests, and formal verifications, then check traceability with rb spec.
---

# Spec traceability

Traceability links the functional coverage items in `specs.yaml` to models in `models.yaml` and to verification entries in `tests.yaml` or `fpv.yaml`. These fields do not affect execution. `rb spec` reports what is linked and what is not.

## Define coverage items

Create `spec/<block>/specs.yaml`:

```yaml
rtl-buddy-filetype: spec_config

blocks:
  - name: my_block
    desc: Brief description
    docs: [README.md, behavior.md]
    coverage-items:
      - id: MYBLK-COV-01
        desc: Normal operation
      - id: MYBLK-COV-02
        desc: Error recovery
```

IDs are arbitrary strings; a block prefix keeps them unique across the project. One file may define several blocks.

## Link the design model

Point the model at `specs.yaml` with a path relative to `models.yaml`:

```yaml
models:
  - name: my_block
    filelist: [-F my_block.f]
    spec: ../../spec/my_block/specs.yaml
```

With a multi-block spec, the model name selects the block of the same name. A single-block spec matches unconditionally.

## Declare verification coverage

List coverage item IDs under `covers` on simulation tests:

```yaml
tests:
  - name: basic
    model: my_block
    model_path: ../../design/my_block/models.yaml
    testbench: tb_top
    covers: [MYBLK-COV-01, MYBLK-COV-02]
```

Formal verifications use the same field:

```yaml
verifications:
  - name: my_block_safety
    model: my_block
    model_path: ../../design/my_block/models.yaml
    tool: sby
    mode: prove
    covers: [MYBLK-COV-03]
```

Several verifications may cover one item, and one verification may cover several items. Formal suites are found through the project-root `fpv_regression.yaml`.

## Check traceability

Run from the project tree:

```bash
rb spec list
rb spec check-design
rb spec check-coverage
```

- `list` finds blocks under `spec/`, or under `--spec-dir`.
- `check-design` reports whether each block has a linked model. `--design-dir` changes the search root.
- `check-coverage` reports the tests and formal verifications that declare each item. `--verif-dir` changes the simulation-suite search root.

Restrict either check to blocks with `--block`, repeated as needed:

```bash
rb spec check-design --block my_block
rb spec check-coverage --block ip_fifo --block ip_arbiter
```

- An unknown block is a configuration error.
- If a discovered `tests.yaml` cannot load, `check-coverage` reports the suite failure and exits nonzero rather than counting its items as uncovered. Machine output lists these under `suite_load_failures`.

Use `--machine` for structured output. See the [CLI reference](../reference/cli.md) for all options and [YAML formats](../reference/yaml.md) for schemas.

## Query the relationships as a graph

The [design knowledge graph](graph.md) holds the same spec, model, test, and formal-run relationships and uses the same loaders as `rb spec`, so graph queries and traceability checks read the YAML identically.
