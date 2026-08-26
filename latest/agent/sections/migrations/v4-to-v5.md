## v4 to v5

Config-driven commands now anchor managed output on the primary config's directory, the [`command_root`](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/execution-context/).

| Behavior | v4 | v5 |
|----------|----|-----|
| `rtl_buddy.log` location | invocation cwd | command root (`dirname(<primary config>)`) |
| `regression` per-suite cwd | `os.chdir()` into each suite | no chdir; each suite re-anchors its own log |
| `root_config.yaml` discovery | from invocation cwd | from command root |
| `hier` / `axi-profile` default outputs | invocation cwd | resolved command root |
| Coverage `outdir` / `source_roots` | invocation cwd | command root |

Explicit CLI paths still resolve from the invocation directory. Only managed artifacts and default output locations moved.

### Hook scripts run at the invocation directory

`sweep` and `preproc` hooks run from the invocation directory. Build paths from the injected `suite_dir` and `artifact_dir`, never `os.getcwd()`:

```python
out  = os.path.join(artifact_dir, "gen.sv")          # correct
stim = os.path.join(suite_dir, "vectors", "in.txt")  # correct
```

Wrap a third-party generator that only writes relative to the CWD in a temporary `os.chdir(suite_dir)`. Repoint CI from the invocation directory to the command root for `rtl_buddy.log`.
