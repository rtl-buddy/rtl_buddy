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

An item may carry project metadata, such as the test tier that must own it, under `meta`:

```yaml
      - id: MYBLK-COV-01
        desc: Normal operation
        meta: {tier: pr}
```

`meta` is a free-form mapping that rtl-buddy does not interpret. `rb --machine spec check-coverage` reports it as `meta` on each item that sets it, so a project script can group or filter items without re-reading `specs.yaml`. Dates and other non-JSON values in `meta` are reported as strings. Other keys beside `id`, `desc` and `meta` are ignored with a warning.
