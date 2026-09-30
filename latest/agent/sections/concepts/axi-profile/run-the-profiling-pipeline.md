## Run the profiling pipeline

The four stages run in this order. Discovery and monitor generation take a model; trace ingestion and notebook launch take a test.

```bash
rb axi-profile discover soc_top
rb axi-profile gen-monitor soc_top --time-precision 1ps
rb test my_test
rb axi-profile run my_test --emit-txns-parquet
rb axi-profile notebook my_test
```
