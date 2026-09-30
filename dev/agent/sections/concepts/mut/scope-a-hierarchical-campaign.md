## Scope a hierarchical campaign

An empty `scope` mutates only `design_file` and does not need `rtl-buddy-view`. A non-empty `include` or `exclude` resolves files through the hierarchy graph, so `rtl-buddy-view` must be on `PATH`.

- Patterns are case-sensitive `fnmatch` shell globs. `**` is not recursive; spell out path segments.
- Patterns match both instance paths and source paths, including model-relative and absolute source paths.
- An empty `include` selects all hierarchy files; matching `exclude` entries remove files. A scope that selects no files is fatal.
- Mutation is file-based: a module instantiated several times is mutated once, in its source file.
- `schedule: sequential` processes scoped files in sorted order; `round_robin` interleaves them.
- `per_file_cap` limits each file and `max_mutants` limits the campaign. `design_file` stays the baseline target.
