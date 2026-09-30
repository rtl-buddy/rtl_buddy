## Pin the source revision

A `source.git_sha` in the manifest is recorded verbatim. Otherwise `cfg-xplr.commit-mode` decides:

- `auto` records `HEAD` when the configured source scope is clean. If it is dirty, xplr snapshots the scope onto an `exp/<id>` branch without changing the working tree.
- `self-managed` rejects an uncommitted source scope.

`source.diff_from` defaults to the parent's pinned revision; `--baseline <ref>` overrides it.

The ledger directory, the xplr worktree root and `rtl_buddy.log` are excluded from the dirtiness check and from snapshots. Agent scratch files are not, so keep `artefacts/`, logs, worktrees and temporary manifests gitignored. `register` warns when the ledger or log is inside a repository but not ignored.
