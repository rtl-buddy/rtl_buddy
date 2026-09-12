## rtl-buddy-cdc cannot take a filelist `+incdir+`

rtl-buddy-cdc takes plain source paths and has no include-path option, so a filelist `+incdir+` cannot reach it from `rb cdc` or from the hub's domain-map build; every other non-simulation flow forwards them (Yosys `-I`, Vivado `-include_dirs`). The run logs `cdc.filelist_incdirs_unsupported` naming the directories, and a header that resolves only through one of them fails in the analyzer with `Cannot find include file`. Until the analyzer grows the option, spell the `` `include `` relative to the including file or run the `vivado` cdc tool.
