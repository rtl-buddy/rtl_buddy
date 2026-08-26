## Run the profiling pipeline

Run the four stages in order:

```bash
rb axi-profile discover soc_top
rb axi-profile gen-monitor soc_top --time-precision 1ps
rb test my_test
rb axi-profile run my_test --emit-txns-parquet
rb axi-profile notebook my_test
```

The stages are independent wrappers around the external profiler. Discovery and monitor generation select a model; trace ingestion and notebook launch select a test.
