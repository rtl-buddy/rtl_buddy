## Understand input processing

rtl_buddy resolves the model through the normal filelist loader and reads inputs in this order: design sources, `constraints`, then `properties`. Filelist include directories and defines are passed to the selected frontend. The proof, vacuity pass, and COI analysis use the same sources, defines, frontend, and parameters.

Both frontends define `FORMAL` and do not define `SYNTHESIS`. Define handling is normalized for consistent elaboration:

- User `+define+FORMAL` is dropped with a warning.
- Repeated names use the last value; identical repeats are deduplicated.
- Values containing whitespace are dropped with a warning because Yosys script tokenization cannot represent them safely.

Slang still preprocesses `synthesis translate_off` regions, so includes and macros there must resolve.
