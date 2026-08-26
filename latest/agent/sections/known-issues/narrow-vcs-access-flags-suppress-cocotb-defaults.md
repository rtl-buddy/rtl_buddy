## Narrow VCS access flags suppress cocotb defaults

For cocotb, rtl_buddy adds VPI access unless any configured compile option already starts with `-debug_access` or `+acc`. A narrower configured flag therefore suppresses the full default and may prevent signal writes. Remove the narrow flag or configure sufficient access, such as `-debug_access+all` and `+acc+rw`.
