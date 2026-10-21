---
description: Configure sweep and preprocessing hooks in tests.yaml, including their inputs, paths, output ownership, and failure behavior.
---

# Test plugins

<a id="hooks"></a>

Hooks are Python scripts that customize tests without changing `rtl_buddy`. Configure them per test in `tests.yaml`. A hook runs at module scope and receives predefined variables, not function arguments.

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
| `test_cfg` | The original, immutable `TestConfig`. A copy may change any field except `reglvl`. |
| `root_cfg` | The project `RootConfig`, read-only. Reads and method calls work; setting or deleting an attribute raises `AttributeError`, which fails the hook. |
| `suite_dir` | Absolute directory containing `tests.yaml`. |
| `artifact_dir` | Artefact root for the incoming test name. |
| `out_test_cfgs` | Output list the script must assign. |
| `logger` | The rtl_buddy logger. |
| `__file__` | Absolute path of the hook. |

If the script raises, the source test is a setup failure and the remaining tests continue.

## Modify a test with `preproc`

Preprocessing runs after sweep expansion and before compile. Modify `test_cfg` in place:

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

A `preproc` script receives the sweep variables except `out_test_cfgs`, plus:

| Variable | Value |
|---|---|
| `run_id` | Run index for a dispatched element or a single test. `None` when one invocation serves several local `randtest` runs. |
| `run_artifact_dir` | `artifact_dir/run-NNNN` when `run_id` is set, otherwise `artifact_dir`. Also the simulation working directory. |

Both directories exist before the hook runs. If the script raises, the test is a setup failure and the remaining tests continue.

## Write generated files safely

Pick the output directory by how the data varies:

- Test-invariant output goes in `artifact_dir`. Concurrent dispatched runs share it, so write to a temporary file and publish it with `os.replace()`.
- Run- or seed-specific output goes in `run_artifact_dir`. It is unique only when `run_id` is set. Local `randtest` calls `preproc` once for all seeds, so use dispatch or `sweep` when generated data must differ per seed.

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

Resolve suite inputs from `suite_dir`, never `os.getcwd()`. Plusargs pass through verbatim, so give suite-local input paths explicitly. A relative output path lands in `run_artifact_dir` because simulation runs there.

## Handle hook execution context

Hooks run through `exec()` in the invocation working directory, not the suite directory. `__name__` is `"__rtl_buddy_hook__"`, so an `if __name__ == "__main__":` block never runs; put hook logic at module scope.

`print()` output is captured as `hook.stdout`, shown on stderr, and written to `rtl_buddy.log`. It cannot corrupt `--machine` JSON on stdout. Use `logger` when a message needs a level.

Child-process output is not captured automatically. Capture it and print it from the hook:

```python
res = subprocess.run(cmd, capture_output=True, text=True, check=True)
print(res.stdout, end="")
```

The captured `sys.stdout` has no usable `fileno()` or `.buffer`. If a third-party generator can only write relative to its working directory, `os.chdir(suite_dir)` for the call and restore the previous directory in `finally`.

See [Execution Context](execution-context.md) for path ownership.

## Post-processing

The config loader accepts `postproc`, but custom post-processing hooks do not run. Results come from the built-in post-processing flow.
