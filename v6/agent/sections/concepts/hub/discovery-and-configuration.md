## Discovery and configuration

The hub writes `.rtl-buddy/hub.json` after binding. It contains the PID, TCP address, project root, server version, and optional HTTP port and active model. Peers discover this file by walking upward from their current directory.

Set `RTL_BUDDY_HUB=<host>:<port>` when a peer runs outside the project tree. Use the `tcp` value from `hub.json`; the variable is not a file path.

Optional `.rtl-buddy/hub.toml` settings include:

```toml
[hub]
listen_port = 0
http_port = 0
log_path = ".rtl-buddy/hub.log"

[mapping]
tb_prefix = "tb.dut."
view_json = ".rtl-buddy/view.json"

[[mapping.signal_aliases]]
wave = "tb.legacy_dut.clk"
view = "tb.dut.clk"
```

Port `0` lets the OS choose. Relative paths resolve from the project root. Signal aliases are applied before `tb_prefix` is removed. Validate edits with:

```bash
rb hub config validate
```

Only `[hub]` and `[mapping]` are valid top-level sections. Unknown keys inside those sections are tolerated for forward compatibility.
