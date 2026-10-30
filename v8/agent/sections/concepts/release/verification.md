## Verification

`verify.command` runs in a copy of each stage tree (`src`, `obf`) and in the unpacked tarball (`pkg`), from the release root, with `RELEASE_ROOT` and `RELEASE_STAGE` set. A stage passes when the command exits 0 and its output matches `verify.pass`. When `verify.compare` is set, the lines it matches must be identical in every stage, so a rename that changes behaviour cannot ship. Logs are in `artefacts/<name>-<version>/verify/`.

The flow also checks, independently of the testbench:

- every obfuscated file is the same length as its input, and no renamed original name remains in it;
- every encrypted file holds nothing outside its protected envelope;
- no file to be obfuscated uses a macro string quote, or token pasting that joins two name pieces (`` a``_nxt ``), which the lexical obfuscator renames inconsistently. A double backtick that only delimits an argument (`` ``O``.field ``) is fine. With `obfuscation.token-paste: preserve`, every name a paste can form, its literal pieces and any identifier pasted into it keep their spelling instead; the internal manifest lists them under `token_paste_preserved`.
