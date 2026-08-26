## Pin the source revision

If `source.git_sha` is supplied in the manifest, xplr records it verbatim. Otherwise `cfg-xplr.commit-mode` controls pinning:

- `auto` records `HEAD` when the configured source scope is clean. If dirty, it snapshots the scope onto an `exp/<id>` branch without changing the working tree.
- `self-managed` rejects an uncommitted source scope.

`source.diff_from` defaults to the parent's pinned revision. Override it with `--baseline <ref>`.

The ledger directory, xplr worktree root, and `rtl_buddy.log` are excluded from source dirtiness and automatic snapshots. Agent scratch files are not; keep `artefacts/`, logs, worktrees, and temporary manifests gitignored. `register` warns when the ledger or log is inside a repository but not ignored.
