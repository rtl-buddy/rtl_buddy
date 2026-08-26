## Record and machine contracts

Each `record.json` validates against the bundled draft-2020-12 schema `rtl_buddy/xplr/xplr-experiment-1.0.json`. The main blocks are `source`, `knobs`, optional `config_snapshot`, `outcome`, and `provenance`; the record also carries `schema_version`, id, optional parent, and hypothesis.

Every `rb --machine xplr ...` command prints one [machine envelope](https://rtl-buddy.github.io/rtl_buddy/dev/agents/#machine-mode). Exit 0 means success. Exit 2 reports user or schema errors as `payload.error`. Optional payload keys may be added in minor releases; removing or changing record fields requires a schema-version change.
