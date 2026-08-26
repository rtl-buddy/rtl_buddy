## Materialize revisions and reclaim disk

Every pinned experiment can be recreated as a detached worktree:

```bash
rb xplr materialize exp-0003
rb xplr release exp-0003
rb xplr gc --dry-run
rb xplr gc --policy keep-frontier --target-gb 40
```

`materialize` is idempotent and defaults to `artefacts/xplr/worktrees/<id>`. `release` removes that worktree but keeps the source branch and experiment record.

Garbage collection always preserves `record.json`. The default `keep-frontier` policy also protects frontier members, their direct lineage, and non-terminal experiments; eligible worktrees and listed outcome artefacts are removed oldest-first until usage is below the target. `register` automatically invokes the configured policy above the high watermark and blocks only when a hard-cap overrun cannot be reclaimed.
