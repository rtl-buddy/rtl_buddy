## Flow provenance

Flow ownership comes from each flow's regression manifest, first at the project root and then at the path configured in [`cfg-rtl-reg`](https://rtl-buddy.github.io/rtl_buddy/dev/reference/yaml/#root_configyaml). A missing manifest means the project does not use that flow; an invalid manifest is reported. Unclaimed `tests.yaml` suites default to `sim`.
