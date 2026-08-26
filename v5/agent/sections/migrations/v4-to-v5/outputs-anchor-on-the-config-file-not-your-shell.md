## Outputs anchor on the config file, not your shell

v5 introduces [`ExecutionContext`](https://rtl-buddy.github.io/rtl_buddy/v5/concepts/execution-context/): every config-driven command anchors its outputs on the directory containing its primary config (the **command root**), regardless of where you invoke `rb` from. Previously some flows wrote relative to the invocation directory, which scattered scratch files into whatever tree you happened to be standing in.

| Behavior | v4 | v5 |
|----------|----|-----|
| `rtl_buddy.log` location | invocation cwd | command root (`dirname(<primary config>)`) |
| `regression` per-suite cwd | `os.chdir()` into each suite | no chdir; each suite re-anchors its own file log |
| `root_config.yaml` discovery | walks up from invocation cwd | walks up from command root |
| `hier`, `axi-profile` *default* outputs / artefacts | invocation cwd | resolved config's command root |
| Coverage `outdir` / `source_roots` | invocation cwd | command root |

For most projects this is transparent — artefacts simply land in the predictable place (under the suite's `artefacts/`) whether you run from the suite directory or the repo root.

**Explicit output paths are unchanged.** A value you pass on the command line — `hier -o diagram.svg`, `axi-profile ... -o report.html`, `filelist <model> out.f` — still resolves relative to your shell's cwd (`invocation_cwd`), matching normal shell behavior. Only the command-managed artefacts and default output locations moved to the command root. If your CI passes `-o`, keep looking where you told it to write; do not redirect those to `dirname(models.yaml)`/`dirname(tests.yaml)`.
