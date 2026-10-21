## rtl-buddy-cdc cannot take a filelist `+incdir+`

rtl-buddy-cdc has no include-path option, so `rb cdc` and the hub's domain-map build cannot pass it `+incdir+` entries. The run warns `cdc.filelist_incdirs_unsupported` and names the directories. A header that resolves only through one of them fails with `Cannot find include file`. Write the `` `include `` path relative to the including file, or use the `vivado` cdc tool.
