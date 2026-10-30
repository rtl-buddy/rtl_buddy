## Issue Triage

Set these GitHub fields on every issue:

- **Type** — the org-level Issue Type: `Bug`, `Feature`, or `Docs`. Set once on every issue.
- **Priority** — the org-level Issue Field: `Urgent`, `High`, `Medium`, or `Low`. Reflects how soon the work should land, not how big it is.
- **Effort** — the org-level Issue Field: `High`, `Medium`, or `Low`. Optional; fill it in when the answer is non-obvious.

Type, Priority, and Effort are fields, not labels. Apply one preferred `area/*` label when possible. Edit the shared taxonomy in `.github/labels.json` and run `.github/sync-labels.sh`; do not create labels by hand.

| Label | Covers |
|---|---|
| `area/test` | `test`, `randtest`, `regression`, and the compile/sim runner pipeline |
| `area/wave` | waveform viewing and integration (surfer, WCP) |
| `area/fpv` | formal property verification (`rb fpv`, sby plus commercial backends) |
| `area/abv` | assertion-based verification (SVA, properties) in sim |
| `area/mut` | mutation testing (`rb mut`) |
| `area/pd` | ASIC physical design: `synth`, `pnr`, `power` |
| `area/fpga` | FPGA implementation flow (`rb fpga`, Vivado + open backends) and FPGA-specific checks |
| `area/hier` | `hier` viewer and `rtl-buddy-view` integration |
| `area/axi-profile` | `axi-profile` discover, run, notebook, and monitor generation |
| `area/hub` | the hub server, marimo integration, hub event plumbing |
| `area/skill` | the bundled agent skill and `skill install` |
| `area/workflow` | spec-driven / end-to-end workflow orchestration |
| `area/config` | `root_config.yaml`, suite YAML loading, `filelist`, and model resolution |
| `area/tooling` | `tool-check`, `tool_manifest.py`, and external-tool integration |
| `area/infra` | CI workflows, packaging, release mechanics, dependencies, machine-mode logging, and the rtl-buddy CLI |

Use `discussion` for scope or design conversations. Reserve `version/*` labels for PRs.
