## Find each command root

| Command | Command root | Artefact or tool directory |
| --- | --- | --- |
| `test`, `randtest`, `wave` | Directory containing `tests.yaml` | `artefacts/<test>[/run-NNNN]` |
| `regression` | Directory containing `regression.yaml` | Each suite's own artefact tree |
| `synth`, `fpv`, `pnr`, `power` | Directory containing that flow's YAML | `artefacts/<run>` |
| `mut` | Directory containing `mut.yaml` | `artefacts/mut/<campaign>` |
| `hier --view dut` | Directory containing `models.yaml` | `artefacts/hier/<model>` |
| `hier --view tb` | Directory containing `tests.yaml` | `artefacts/hier/<model>/tb/<testbench>` |
| `axi-profile run` | Directory containing `tests.yaml` | `artefacts/axi/<test>` |
| `axi-profile discover` | Directory containing `models.yaml` | `artefacts/axi/<model>` |
| `filelist`, `saif` | Config root for reads; shell CWD for explicit output | Explicit output path |
| `hub` | Project root | `.rtl-buddy/` |

External tools run inside the listed artefact directory. A regression re-anchors each suite's outputs and log to that suite, then writes its final log and merged outputs beside `regression.yaml`.
