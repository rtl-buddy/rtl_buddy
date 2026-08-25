---
description: Configure sweep and preprocessing hooks in tests.yaml, including their inputs, paths, output ownership, and failure behavior.
---

# Test plugins

<a id="hooks"></a>

Tests can run Python hooks without changing `rtl_buddy`. Configure hooks per test in `tests.yaml`; hook scripts execute at module scope and receive predefined variables rather than function arguments.

## Expand tests with `sweep`

A sweep runs before the test flow and replaces one test with a list of variants. Assign the variants to `out_test_cfgs`:

```yaml
- name: sweep_case
  sweep:
    path: example_sweep.py
  model: my_design
  model_path: ../src/models.yaml
  testbench: tb_top
  reglvl: 2000
```

```python
out_test_cfgs = []
for i in range(4):
    cfg = test_cfg.copy()
    cfg.name = f"{test_cfg.name}_{i}"
    cfg.plusargs["SCENARIO"] = str(i)
    out_test_cfgs.append(cfg)
```

| Variable | Value |
|---|---|
| `test_cfg` | Original immutable `TestConfig`; copied variants may change any field except `reglvl` |
| `root_cfg` | Mutable `RootConfig` |
| `suite_dir` | Absolute directory containing `tests.yaml` |
| `artifact_dir` | Artefact root for the incoming test name |
| `out_test_cfgs` | Output list the script must assign |
| `logger` | rtl_buddy logger |
| `__file__` | Absolute hook path |

A script exception marks the source test as a setup failure; remaining tests continue.

## Modify a test with `preproc`

Preprocessing runs after sweep expansion and before compile. Modify `test_cfg` directly:

```yaml
- name: basic
  preproc:
    path: my_preproc.py
  model: my_design
  model_path: ../src/models.yaml
  testbench: tb_top
```

```python
import os
from pathlib import Path

test_cfg.plusargs["BUILD_ID"] = os.environ.get("CI_BUILD_ID", "local")
test_cfg.plusargs["stimulus"] = str(
    Path(suite_dir) / "vectors" / "streaming_contract.txt"
)
```

The script receives `test_cfg`, `root_cfg`, `suite_dir`, `artifact_dir`, `logger`, and `__file__`, plus:

| Variable | Value |
|---|---|
| `run_id` | Run index for a dispatched element or single test; `None` when one hook invocation serves several local `randtest` runs |
| `run_artifact_dir` | `artifact_dir/run-NNNN` when `run_id` is set; otherwise `artifact_dir`. This is also the simulation working directory |

Both artefact directories exist before the hook runs. A script exception marks the affected test as a setup failure; remaining tests continue.

## Write generated files safely

Choose the output directory from the data's lifetime:

- Write test-invariant output to `artifact_dir`. Concurrent dispatched runs share this directory, so publish files atomically with a temporary file and `os.replace()`.
- Write run- or seed-specific output to `run_artifact_dir`. It is unique only when `run_id` is set. Local `randtest` invokes preproc once for all seeds, so use dispatch or sweep when generation must vary per seed.

```python
import os
from pathlib import Path

if run_id is not None:
    (Path(run_artifact_dir) / "stimulus.hex").write_text(generate(run_id))
else:
    out = Path(artifact_dir) / "stimulus.hex"
    tmp = out.with_name(f"{out.name}.{os.getpid()}.tmp")
    tmp.write_text(generate())
    os.replace(tmp, out)
```

Resolve suite inputs from `suite_dir`; do not use `os.getcwd()`. Plusargs are passed verbatim, so make suite-local input paths explicit. Relative output paths may target `run_artifact_dir` because simulation runs there.

## Handle hook execution context

Hooks run through `exec()` in the invocation working directory, not the suite directory. `__name__` is `"__rtl_buddy_hook__"`, so place hook logic at module scope; an `if __name__ == "__main__":` branch is skipped.

Hook `print()` output is captured as `hook.stdout`, appears on stderr and in `rtl_buddy.log`, and cannot corrupt `--machine` JSON on stdout. Prefer `logger` when a message needs a level.

Child-process output is not captured automatically. Capture it and print it through the hook:

```python
res = subprocess.run(cmd, capture_output=True, text=True, check=True)
print(res.stdout, end="")
```

The captured `sys.stdout` has no usable `fileno()` or `.buffer`. If a third-party generator can only write relative to its working directory, change to `suite_dir` temporarily and restore the prior directory in `finally`.

See [Execution Context](execution-context.md) for path ownership rules.

## Post-processing

`postproc` is accepted by the configuration loader, but custom post-processing hooks are not executed. Use the built-in post-processing flow.
