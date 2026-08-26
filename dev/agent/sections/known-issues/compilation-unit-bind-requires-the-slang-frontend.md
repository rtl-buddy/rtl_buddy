## Compilation-unit bind requires the slang frontend

Yosys's native `verilog` frontend does not resolve a top-level `bind`, so no formal cells elaborate. rtl_buddy fails a property-based proof that would otherwise pass vacuously. Set `frontend: slang` and configure the yosys-slang plugin. Inline assertions do not need this guard. See [Formal Property Verification](https://rtl-buddy.github.io/rtl_buddy/dev/concepts/fpv/).
