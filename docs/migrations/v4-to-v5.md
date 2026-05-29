---
description: How to migrate an rtl_buddy project from v4 to v5, covering the ExecutionContext change that anchors all outputs on the primary config file.
---

# Migrating from v4 to v5

## Outputs anchor on the config file, not your shell

v5 introduces [`ExecutionContext`](../concepts/execution-context.md): every config-driven command anchors its outputs on the directory containing its primary config (the **command root**), regardless of where you invoke `rb` from. Previously some flows wrote relative to the invocation directory, which scattered scratch files into whatever tree you happened to be standing in.

| Behavior | v4 | v5 |
|----------|----|-----|
| `rtl_buddy.log` location | invocation cwd | command root (`dirname(<primary config>)`) |
| `regression` per-suite cwd | `os.chdir()` into each suite | no chdir; each suite re-anchors its own file log |
| `root_config.yaml` discovery | walks up from invocation cwd | walks up from command root |
| `hier`, `axi-profile` *default* outputs / artefacts | invocation cwd | resolved config's command root |
| Coverage `outdir` / `source_roots` | invocation cwd | command root |

For most projects this is transparent — artefacts simply land in the predictable place (under the suite's `artefacts/`) whether you run from the suite directory or the repo root.

**Explicit output paths are unchanged.** A value you pass on the command line — `hier -o diagram.svg`, `axi-profile ... -o report.html`, `filelist <model> out.f` — still resolves relative to your shell's cwd (`invocation_cwd`), matching normal shell behavior. Only the command-managed artefacts and default output locations moved to the command root. If your CI passes `-o`, keep looking where you told it to write; do not redirect those to `dirname(models.yaml)`/`dirname(tests.yaml)`.

## Hook scripts run at the invocation directory

This is the one change most likely to break existing projects.

`sweep` and `preproc` hooks execute via `exec()` inside the `rb` process and share its working directory. **The change is specific to `regression`:** in v4 it did `os.chdir()` into each suite, so hooks ran from the suite directory; v5 removes that chdir, so hooks now run at `invocation_cwd` — your shell's directory — like every other command.

Single-suite `test` and `randtest` are unaffected here: their hook working directory was already `invocation_cwd` in v4, so nothing changes for them. (Their *artefact* locations do move under the suite in v5 — see the table above — but that is anchored independently of the hook's cwd.)

In all cases, hooks receive `suite_dir` and `artifact_dir` as namespace variables. Build paths from those:

```python
# inside a sweep / preproc script
import os
out = os.path.join(artifact_dir, "gen.sv")          # correct
stim = os.path.join(suite_dir, "vectors", "in.txt")  # correct
out = os.path.join(os.getcwd(), "gen.sv")            # wrong — invocation cwd
```

Any hook that called `os.getcwd()` (directly or indirectly) to find the suite will break.

### Third-party generators that write relative to cwd

If a hook delegates to a generator you don't control — one that writes its outputs relative to `os.getcwd()` and exposes no output-directory parameter — `suite_dir`/`artifact_dir` can't help, because you can't tell the generator where to write. Wrap the call in a `chdir` to the suite (restore afterwards):

```python
prev = os.getcwd()
os.chdir(suite_dir)
try:
    gen_dir = third_party_generate(...)   # writes relative to cwd
finally:
    os.chdir(prev)
```

See [Quirks & Known Issues](../known-issues.md) for the failure signature when this is missed.

## What to update

### CI scripts

Scripts that looked for `rtl_buddy.log` in the invocation directory should look under the command root (`dirname(<primary config>)`) instead.

### Hook scripts

Replace any `os.getcwd()` usage with `suite_dir` or `artifact_dir`. For uncontrollable generators, use the `chdir(suite_dir)` pattern above.
