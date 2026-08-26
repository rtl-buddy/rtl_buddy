## Spec traceability commands

The `rb spec` commands check the spec-to-test traceability layer. They do not affect simulation and are safe to run at any time:

```bash
# List all spec blocks discovered in the project
rtl-buddy --machine spec list

# Check which spec blocks have a linked design model (models.yaml spec: pointer)
rtl-buddy --machine spec check-design

# Check which coverage items are addressed by at least one test (tests.yaml covers:)
rtl-buddy --machine spec check-coverage
```

In machine mode, `spec list` returns `{"blocks": [...]}` and `spec check-coverage` returns `{"items": [...]}` with a `"covered": true/false` field per item. Use these to identify uncovered items programmatically.

All three commands default to searching `spec/`, `design/`, and `verif/` under the project root. Pass `--spec-dir`, `--design-dir`, or `--verif-dir` to narrow the scope.
