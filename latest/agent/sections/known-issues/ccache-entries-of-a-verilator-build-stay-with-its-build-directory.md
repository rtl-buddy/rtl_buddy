## ccache entries of a Verilator build stay with its build directory

rb sets `CCACHE_NAMESPACE` to the absolute build directory of each compile of a Verilator builder (an executable named `verilator*`, which rb passes that directory as `--Mdir`, or `simulator-family: verilator`), unless the environment already sets it.

- Verilator's `verilated.mk` compiles through ccache (`OBJCACHE ?= ccache` when Verilator's build found ccache) and, for a design that splits into several C++ files, builds a precompiled header. Clang records the absolute paths of the headers in it, and ccache does not hash the build directory.
- Without a namespace, a build in another directory (another test, a shared build, another checkout) can take that header from the cache. Once the first directory is rebuilt with another design, the build stops with `fatal error: file '<other dir>/V<top>__Syms.h' has been modified since the precompiled header 'V<top>__pch.h.fast.gch' was built`.
- The cost: ccache reuses Verilator's runtime objects (`verilated.o` and the like) only within one build directory, not across them.
- ccache before 4.8 ignores `CCACHE_NAMESPACE` and can still fail this way. There, set `CCACHE_DISABLE=1` for `rb test`.
- An empty `CCACHE_NAMESPACE=` in the environment shares the cache across directories again, with the failure above.

rb drops the namespace once Verilator or ccache keeps a precompiled header to the directory that built it; rtl_buddy#1083 tracks that.
