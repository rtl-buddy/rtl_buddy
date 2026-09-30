## Discovery and configuration

The running hub writes `.rtl-buddy/hub.json` with its PID, TCP address and HTTP port. Peers find it by walking up from their current directory. A peer outside the project tree sets `RTL_BUDDY_HUB=<host>:<port>` to the `tcp` value from `hub.json`; it is an address, not a file path.

Optional `.rtl-buddy/hub.toml`:

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

Port `0` lets the OS choose. Relative paths resolve from the project root. Signal aliases apply before `tb_prefix` is removed. Only `[hub]` and `[mapping]` are accepted: an unknown top-level section is an error, and unknown keys inside them are ignored. Check edits with `rb hub config validate`.
