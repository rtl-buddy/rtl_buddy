## Scope a hierarchical campaign

An empty `scope` mutates only `design_file` and does not require `rtl-buddy-view`. A non-empty `include` or `exclude` resolves files through the hierarchy graph, so `rtl-buddy-view` must be on `PATH`.

Scope patterns:

- Use case-sensitive `fnmatch` shell globs. `**` is not recursive; spell out path segments.
- Match both instance paths and source paths, including model-relative and absolute source paths.
- An empty `include` selects all hierarchy files; matching `exclude` entries remove files.
- A scope that selects no files is fatal.

Mutation remains file-based: a module instantiated several times is mutated once in its source file. Scoped files are processed in sorted order for `schedule: sequential`; `round_robin` interleaves files. `per_file_cap` limits each file, while `max_mutants` limits the campaign globally. Under a non-empty scope, the selected files are mutated and `design_file` remains the baseline target.
