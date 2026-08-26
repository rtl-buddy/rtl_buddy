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
