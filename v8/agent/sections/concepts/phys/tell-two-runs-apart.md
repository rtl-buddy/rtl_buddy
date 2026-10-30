## Tell two runs apart

Both producers record what shaped a run beside what it measured, so two runs of one design are two measurements. A model written by an older rtl-buddy lacks these fields and shows them as not recorded.

- **Power mode and activity.** `rb power` records its mode (`static` or `dynamic`) and its activity source: `default`, `synthetic`, `saif` or `vcd`. For a trace it also records the path, sha256, the test that produced it (derived from the artefact layout) and the scope. For `synthetic` it records the toggle rate and duty. A trace re-captured under the same path has a different sha256 and counts as a different measurement.
- **Config fingerprint.** Both flows record the platform, effort, constraints file and its sha256, and a short digest of the effective tool options, for example `nangate45 · timing-opt · sdc a1b2c3d4 · opts 5655beea1f20`. The digest covers only settings the backend uses, so equivalent spellings match. Power runs also identify the netlist or routed database they measured.
