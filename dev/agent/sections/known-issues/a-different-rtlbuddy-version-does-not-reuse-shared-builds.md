## A different rtl_buddy version does not reuse shared builds

Build stamps written by another rtl_buddy version do not validate, so each build directory recompiles once, and repeatedly while hosts of one cluster run different versions. Use `--rebuild` to compile regardless, not a manual delete of `artefacts/.shared-builds/`.
