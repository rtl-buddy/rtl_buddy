## Avoid frontend-specific property failures

- Prefer packed checker storage when a property uses a variable index. A variable index into an unpacked array fails because the array elaborates as a memory.
- Verify that `(* anyconst *)` produces an `$anyconst` cell in your frontend; some builds drop it. See [Known Issues](https://rtl-buddy.github.io/rtl_buddy/v6/known-issues/#verify-that-anyconst-elaborates).
- Replace unsupported `$past` or `$stable` uses with explicit history registers when equivalent.
- A slang `bind` may connect checker ports to DUT ports, interface members, internal nets, and parameters. Checker parameters should be passed from the target scope rather than hard-coded.

<a id="reduced-configuration-proofs"></a>
