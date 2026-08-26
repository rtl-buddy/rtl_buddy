## Generated `run.f` files are checkout-specific

rtl_buddy writes explicit source entries as absolute paths so Verilator cannot resolve a relative source through an include or library directory in another checkout. Do not commit or copy `run.f` between checkouts. Use one symlink spelling of a checkout consistently, because path spelling affects compile keys.

On a cluster with different mount paths per node, a stamp from one node may not validate on another. This causes a safe recompile, not compilation of the wrong source.
