## Customer registers

The `csr:` section ships the registers software may use, generated from the design's SystemRDL. It needs the `release-csr` extra (`uv add 'rtl_buddy[release-csr]'`).

Each entry in `csr.windows` is one address map (`rdl`, `top`) at an absolute `base`. Several windows may share one RDL, as alias decoders of the same register block do. A register ships when both hold:

- it is eligible: the `gate` user-defined property is true on it, or on an enclosing regfile or memory. With no `gate`, every register is eligible. An internal register never ships, whatever the whitelist says;
- a `csr.registers` entry matches it, or an enclosing regfile or memory. `match` is a glob on `<window>.<path>`, the instance names joined by `.` without array indices, so `dma.ch.*` takes every register of the `ch` regfile array.

Every register and memory not selected is removed, and so is every signal. User-defined properties never ship, because they are internal annotations. A reference whose target was removed is dropped (the log lists them as `release.csr_refs_dropped`). The shipped RDL gives every instance its explicit offset, so removing a register never moves another. An entry that ships nothing, a window left empty, and an `obfuscate-fields` pattern that matches no field are all errors.

`obfuscate-fields` lists field-name globs to hide in the registers the entry selects. Each such field keeps its bit position, width, access and reset value, so software can still write the register while preserving the field. It ships as `f<lsb>` (`f4` for bits `[7:4]`), with no `name`, `desc` or `encode`.

The map ships clear in `csr/`, under `csr.name` (default `<name>_csr`):

| File | Contents |
|---|---|
| `<csr name>.rdl` | Each window's selected registers, and a top address map `<csr name>` that places every window at its base. The flow compiles this file on its own before generating the others from it |
| `<csr name>.h` | C header from PeakRDL cheader: register structs and per-field `_bm`, `_bp`, `_bw` and `_reset` macros |
| `<csr name>.svh` | `` `define <prefix>_<WINDOW>_<PATH> `` with the absolute address, plus `_RESET` and per-field `_<FIELD>_LSB` and `_<FIELD>_WIDTH`; array elements carry `_<index>` |
| `<csr name>.md` | Register map: address, reset value and fields per register |

The RDL sources and the files they `` `include `` are release inputs, so they must be committed. The internal manifest records the register count per window and the digest of each `csr/` file, and `--reproduce` requires them unchanged. `rb release --csr-only` writes only `artefacts/<name>-<version>/csr/` and needs neither Verible nor VCS. Use it to regenerate anything derived from the map, such as testbench register scripts.
