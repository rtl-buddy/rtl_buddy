## Verify that `anyconst` elaborates

Some yosys-slang builds drop `(* anyconst *)` without producing a `$anyconst` cell. The signal then varies freely each cycle and can invalidate symbolic-index proofs. Check the elaborated design before relying on it:

```bash
yosys -p 'read_slang ...; prep -top dut; select -assert-min 1 t:$anyconst'
```

Use a behavioral reference model when portable data-integrity checking matters.
