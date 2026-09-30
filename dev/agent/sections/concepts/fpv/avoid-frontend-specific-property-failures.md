## Avoid frontend-specific property failures

- Use packed checker storage for a variable index. A variable index into an unpacked array fails because the array elaborates as a memory.
- Check that `(* anyconst *)` produces an `$anyconst` cell in your frontend; some builds drop it. See [Known Issues](https://rtl-buddy.github.io/rtl_buddy/dev/known-issues/#verify-that-anyconst-elaborates).
- Replace unsupported `$past` or `$stable` uses with explicit history registers.
- A slang `bind` can connect checker ports to DUT ports, interface members, internal nets and parameters. Pass checker parameters from the target scope instead of hard-coding them.
