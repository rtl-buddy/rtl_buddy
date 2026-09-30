## v4 to v5

Managed output is anchored on the primary config's directory, the [`command_root`](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/execution-context/).

| Behavior | v4 | v5 |
|----------|----|-----|
| `rtl_buddy.log` location | invocation cwd | command root (`dirname(<primary config>)`) |
| `regression` per-suite cwd | `os.chdir()` into each suite | no chdir; each suite re-anchors its own log |
| `root_config.yaml` discovery | from invocation cwd | from command root |
| `hier` / `axi-profile` default outputs | invocation cwd | command root |
| Coverage `outdir` / `source_roots` | invocation cwd | command root |

Explicit CLI paths still resolve from the invocation directory. Only managed artifacts and default output locations moved. Point CI at the command root for `rtl_buddy.log`.

### Hook scripts run at the invocation directory

`sweep` and `preproc` hooks run from the invocation directory. Build paths from the injected `suite_dir` and `artifact_dir`, never `os.getcwd()`:

```python
out  = os.path.join(artifact_dir, "gen.sv")
stim = os.path.join(suite_dir, "vectors", "in.txt")
```

If a third-party generator writes only relative to the working directory, wrap the call in a temporary `os.chdir(suite_dir)`.
