## Multi-block specs.yaml

A single `specs.yaml` can contain multiple blocks — useful when a directory holds several closely related IP:

```yaml
blocks:
  - name: "ip_cdc_sync"
    desc: "Multi-flop level synchronizer"
    coverage-items:
      - id: "CDCSYNC-COV-01"
        desc: "..."

  - name: "ip_cdc_handshake"
    desc: "Four-phase request/acknowledge CDC primitive"
    coverage-items:
      - id: "CDCHS-COV-01"
        desc: "..."
```

Each design model still points to the same `specs.yaml`; the model `name` selects the correct block.
