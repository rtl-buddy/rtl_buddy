## Parser frontend

`--frontend` is forwarded as-is to `rtl-buddy-view`. The default frontend ships with the renderer; `--frontend slang` uses pyslang for SystemVerilog constructs the default frontend doesn't parse. rtl_buddy does not validate the set of accepted frontends — that lets the renderer add frontends without an rtl_buddy release. Unknown values are rejected by the renderer's own argument parser.
