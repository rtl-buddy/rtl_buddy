## Authoring checklist for new suites

1. Add or verify the model entry in `models.yaml`.
2. Add a `testbench` entry and verify the filelist paths resolve correctly.
3. Add at least one test entry with `model`, `model_path`, and `testbench`.
4. Set `reglvl` policy: `0` for must-run sanity tests, larger values for extended tests.
5. Add the suite path to `regression.yaml`.
6. Run a smoke pass:

   ```bash
   rtl-buddy --machine test <name> -c <suite>/tests.yaml
   rtl-buddy --machine regression -c <regression.yaml> -s 0 -l 0
   ```
