## Use saved coverage artefacts

Every coverage run writes `<command root>/cov_dir/manifest.json`, even without merging, and `cov_dir/coverage-model.json` with the per-file, per-module and per-point detail. `rb cov` and the hub read these; you do not need to open them.

Toggle, expression and labeled cover detail need raw Verilator databases. Without them the model falls back to LCOV and holds only unnamed line and branch data.
