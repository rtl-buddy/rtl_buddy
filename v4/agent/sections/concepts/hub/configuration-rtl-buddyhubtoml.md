## Configuration (`.rtl-buddy/hub.toml`)

Optional; sensible defaults apply when the file is absent. Two top-level sections:

```toml
[hub]
listen_port = 0          # 0 = OS-assigned (default). Pin to a specific port to survive across restarts.
http_port   = 0          # Same, for the viewer HTTP+WS layer (only used with --serve-viewer).
log_path    = ".rtl-buddy/hub.log"   # Relative paths resolve from the project root.

[mapping]
tb_prefix   = "tb.dut."  # Stripped from wave-side signal paths before resolving to the view.
view_json   = ".rtl-buddy/view.json"  # Snapshot the resolver consumes. Defaults shown.

# Optional pre-strip aliases — applied before tb_prefix is stripped.
[[mapping.signal_aliases]]
wave = "tb.legacy_dut.clk"
view = "tb.dut.clk"
```

Unknown top-level sections fail validation (typo guard). Unknown keys *inside* known sections are tolerated for forward-compat. `rb hub config validate` runs the same loader and reports errors with file:line context.
