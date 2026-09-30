## Record and machine contracts

Each `record.json` validates against the bundled draft-2020-12 schema `rtl_buddy/xplr/xplr-experiment-1.0.json`. Its main blocks are `source`, `knobs`, optional `config_snapshot`, `outcome` and `provenance`, plus `schema_version`, the id, an optional parent and the hypothesis.

Every `rb --machine xplr ...` command prints one [machine envelope](https://rtl-buddy.github.io/rtl_buddy/dev/agents/#machine-mode). Exit 0 is success; exit 2 reports user or schema errors in `payload.error`. Minor releases may add optional payload keys; removing or changing record fields requires a schema-version change.
