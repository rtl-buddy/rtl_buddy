## Issue Triage

Issues are classified along three axes that live on GitHub itself, not in this repo:

- **Type** — the org-level Issue Type: `Bug`, `Feature`, or `Docs`. Set once on every issue.
- **Priority** — the org-level Issue Field: `Urgent`, `High`, `Medium`, or `Low`. Reflects how soon the work should land, not how big it is.
- **Effort** — the org-level Issue Field: `High`, `Medium`, or `Low`. Optional; fill it in when the answer is non-obvious.

Type, Priority, and Effort are not labels.
They are GitHub Issue Fields configured at the `rtl-buddy` organization level and are queryable via the REST and GraphQL APIs.

Area is captured with `area/*` labels, kept consistent across all rtl-buddy repos.
The taxonomy is defined once in `.github/labels.json` and propagated to every repo with `.github/sync-labels.sh` — edit the JSON and re-run the script rather than creating labels by hand.
Pick one or more from the table below; an issue with no area label is fine for cross-cutting work but a single area is preferred when one fits.

| Label | Covers |
|---|---|
| `area/test` | `test`, `randtest`, `regression`, and the compile/sim runner pipeline |
| `area/wave` | waveform viewing and integration (surfer, WCP) |
| `area/cdc` | clock-domain crossing analysis (`rb cdc`) |
| `area/fpv` | formal property verification (`rb fpv`, sby plus commercial backends) |
| `area/abv` | assertion-based verification (SVA, properties) in sim |
| `area/mut` | mutation testing (`rb mut`) |
| `area/pd` | physical design: `synth`, `pnr`, `power`, and other implementation flows |
| `area/hier` | `hier` viewer and `rtl-buddy-view` integration |
| `area/axi-profile` | `axi-profile` discover, run, notebook, and monitor generation |
| `area/hub` | the hub server, marimo integration, hub event plumbing |
| `area/skill` | the bundled agent skill and `skill install` |
| `area/workflow` | spec-driven / end-to-end workflow orchestration |
| `area/config` | `root_config.yaml`, suite YAML loading, `filelist`, and model resolution |
| `area/tooling` | `tool-check`, `tool_manifest.py`, and external-tool integration |
| `area/infra` | CI workflows, packaging, release mechanics, dependencies, machine-mode logging, and the rtl-buddy CLI |

One extra label exists outside the area set:

- `discussion` — for issues that are scope or design conversations rather than tracked work.

The `version/patch`, `version/minor`, and `version/major` labels are reserved for PRs and drive the release workflow.
Do not apply them to issues.
