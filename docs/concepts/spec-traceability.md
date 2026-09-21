---
description: Link specification items to design models, simulation tests, and formal verifications, then check traceability with rb spec.
---

# Spec traceability

Traceability links functional coverage items in `specs.yaml` to models in `models.yaml` and verification entries in `tests.yaml` or `fpv.yaml`. These fields do not affect execution.

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

IDs are arbitrary strings. Use a block prefix to keep them unique across the project. One file may define several blocks.

## Link the design model

Point the model at `specs.yaml` with a path relative to `models.yaml`:

```yaml
models:
  - name: my_block
    filelist: [-F my_block.f]
    spec: ../../spec/my_block/specs.yaml
```

For a multi-block spec, the model name selects the block with the same name. A single-block spec is matched unconditionally.

## Declare verification coverage

Add coverage item IDs to simulation tests:

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

Multiple verifications may cover one item, and one verification may cover several items. Formal suites are discovered through the project-root `fpv_regression.yaml`.

## Check traceability

Run from the project tree:

```bash
rb spec list
rb spec check-design
rb spec check-coverage
```

- `list` discovers blocks under `spec/` or `--spec-dir`.
- `check-design` reports whether each block has a linked model. Use `--design-dir` to change the search root.
- `check-coverage` reports the tests and formal verifications that declare each item. Use `--verif-dir` to change the simulation-suite search root.

Filter either check to one or more blocks:

```bash
rb spec check-design --block my_block
rb spec check-coverage --block ip_fifo --block ip_arbiter
```

An unknown block is a configuration error. If a discovered `tests.yaml` cannot load, `check-coverage` reports the suite failure and exits nonzero instead of treating its items as uncovered. Machine output includes `suite_load_failures`.

Use the global `--machine` flag for structured output. See the [CLI reference](../reference/cli.md) for all options and [YAML formats](../reference/yaml.md) for schemas.

## Query the relationships as a graph

The [design knowledge graph](graph.md) contains the same spec, model, test, and formal-run relationships. It uses the same loaders as `rb spec`, so graph queries and traceability checks share one interpretation of the YAML.
