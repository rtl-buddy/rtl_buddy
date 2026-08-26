## Planned state

The target distribution mechanism is `uv` with a pinned git reference:

```toml
# pyproject.toml
[tool.uv.sources]
rtl_buddy = { git = "<repo-url>", tag = "v2.0.0" }
```

or equivalently:

```bash
uv add "rtl_buddy @ git+<repo-url>@v2.0.0"
```

This eliminates the submodule and replaces it with a locked dependency in `uv.lock`.
