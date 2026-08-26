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
