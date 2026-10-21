## Narrow VCS access flags suppress cocotb defaults

For cocotb, rtl_buddy adds VPI access unless a configured compile option already starts with `-debug_access` or `+acc`. A narrower flag suppresses the full default and can block signal writes. Remove it, or configure sufficient access such as `-debug_access+all` and `+acc+rw`.
