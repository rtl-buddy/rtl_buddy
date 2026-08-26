## What to update

### CI scripts

Scripts that looked for `rtl_buddy.log` in the invocation directory should look under the command root (`dirname(<primary config>)`) instead.

### Hook scripts

Replace any `os.getcwd()` usage with `suite_dir` or `artifact_dir`. For uncontrollable generators, use the `chdir(suite_dir)` pattern above.
