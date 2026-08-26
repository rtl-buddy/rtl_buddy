## Version cache

Probed versions are cached to `${XDG_CACHE_HOME:-~/.cache}/rtl_buddy/tool_versions.json` keyed by `(path, mtime)`. The cache makes repeated `rb tool-check` invocations cheap — most tools don't need to be re-probed if their binary hasn't changed. Pass `--no-probe-versions` to skip version probing entirely (faster, but the Version column shows `—` for everything).
