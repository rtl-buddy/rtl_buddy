## Per-command mapping

| Command | command_root | artifact_root | External tool CWD |
| --- | --- | --- | --- |
| `test`, `randtest` | `dirname(tests.yaml)` | `<command_root>/artefacts` | `<artifact>/<test>[/run-NNNN]` |
| `regression` | `dirname(regression.yaml)` | each suite's own `artefacts/` | per-suite, same as `test` |
| `wave`, `wave --resim` | `dirname(tests.yaml)` | `<command_root>/artefacts` | `<artifact>/<test>` |
| `synth` | `dirname(synth.yaml)` | `<command_root>/artefacts` | `<artifact>/<synth>` |
| `cdc` | `dirname(cdc.yaml)` | `<command_root>/artefacts` | `<artifact>/<cdc>` |
| `fpv` | `dirname(fpv.yaml)` | `<command_root>/artefacts` | `<artifact>/<fpv>` |
| `pnr` | `dirname(pnr.yaml)` | `<command_root>/artefacts` | `<artifact>/<pnr>` |
| `power` | `dirname(power.yaml)` | `<command_root>/artefacts` | `<artifact>/<power>` |
| `mut` | `dirname(mut.yaml)` | `<command_root>/artefacts` | `<artifact>/mut/<campaign>` |
| `hier --view dut` | `dirname(models.yaml)` | `<model_root>/artefacts/hier/<model>` | `<artifact>` |
| `hier --view tb` | `dirname(tests.yaml)` | `<suite>/artefacts/hier/<model>/tb/<tb_name>` | `<artifact>` |
| `axi-profile run` | `dirname(tests.yaml)` | `<suite>/artefacts/axi/<test>` | `<artifact>` |
| `axi-profile discover` | `dirname(models.yaml)` | `<model_root>/artefacts/axi/<model>` | `<artifact>` |
| `filelist` | `dirname(models.yaml)` (reads) | explicit `-o` / argument | — |
| `saif` | `invocation_cwd` | explicit output argument | — |
| `hub` | project root | `.rtl-buddy/...` | project root |

For a fuller reference (`docs`, `skill`, edge cases), see the [engineering guidelines](https://rtl-buddy.github.io/rtl_buddy/v5/development/guidelines/#command-roots) — the table there is the policy this page describes.
