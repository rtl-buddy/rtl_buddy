## Command Roots

Use these roots unless a command documents a narrower exception:

| Command | Command root | Artifact root | External tool CWD |
|---|---:|---:|---:|
| `test` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | compile: `<artifact>`; sim: `<artifact>` |
| `randtest` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | compile: `<artifact>`; sim: `<artifact>/run-NNNN` |
| `regression` | `dirname(regression.yaml)` | each suite's `<suite>/artefacts/<test>` | same as `test` per suite |
| `elab` | `dirname(models.yaml)` | `<model_root>/artefacts/elab/<model>/<base-or-profile>` | `<artifact>` |
| `elab-regression` | `dirname(elab_regression.yaml)` | each model's `artefacts/elab/<model>/<profile>` | each profile artifact |
| `wave --resim` | `dirname(tests.yaml)` | `<suite>/artefacts/<test>` | same as `test` |
| `synth` | `dirname(synth.yaml)` | `<suite>/artefacts/<synth>` | `<artifact>` |
| `fpv` | `dirname(fpv.yaml)` | `<suite>/artefacts/<fpv>` | `<artifact>` |
| `pnr` | `dirname(pnr.yaml)` | `<suite>/artefacts/<pnr>` | `<artifact>` |
| `power` | `dirname(power.yaml)` | `<suite>/artefacts/<power>` | `<artifact>` |
| `fpga` | `dirname(fpga.yaml)` | `<suite>/artefacts/<fpga>` | `<artifact>` |
| `fpga-regression` | `dirname(fpga.yaml)` | `<suite>/artefacts/<fpga>` | `<artifact>` |
| `hier --view dut` | `dirname(models.yaml)` | `<model_root>/artefacts/hier/<model>` | `<artifact>` |
| `hier --view tb` | `dirname(tests.yaml)` | `<suite>/artefacts/hier/<test-or-model>` | `<artifact>` |
| `axi-profile run` | `dirname(tests.yaml)` | `<suite>/artefacts/axi/<test>` | `<artifact>` |
| `axi-profile notebook` | `dirname(tests.yaml)` | `<suite>/artefacts/axi/<test>` | `<artifact>` |
| `axi-profile discover` | `dirname(models.yaml)` | `<model_root>/artefacts/axi/<model>` | `<artifact>` |
| `axi-profile gen-monitor` | `dirname(models.yaml)` | configured or explicit output; fallback artifact dir | `<artifact>` |
| `graph build` | project root | `<root>/artefacts/graph/` | viewer: `<model_root>/artefacts/hier/<model>` |
| `graph results` / query commands / `mcp` | project root | `<root>/artefacts/graph/` | none, except viewer-backed MCP tools |
| `filelist` | `dirname(models.yaml)` for config reads | explicit output path | no hidden tool CWD |
| `saif` | invocation CWD for explicit paths | explicit output path | no hidden tool CWD |
| `hub` | project root | `.rtl-buddy/...` | project root or `.rtl-buddy`, depending subcommand |
| `docs`, `skill` | no project execution context | none | none |
