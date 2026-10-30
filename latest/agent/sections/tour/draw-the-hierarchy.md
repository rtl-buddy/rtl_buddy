## Draw the hierarchy

`rb hier` renders a model's instance tree from the RTL. The block-diagram form groups siblings and labels the nets between them:

```bash
cd design/demo_tiny_alu_subsys
uv run rb hier demo_tiny_alu_subsys_top
uv run rb hier demo_tiny_alu_subsys_top --format dot --block-diagram | dot -Tpng -o subsys.png
```

```text
demo_tiny_alu_subsys_top
├── u_csr : demo_tiny_alu_subsys_csr
├── u_hs_cmd : ip_cdc_handshake
├── u_afifo : ip_async_fifo
├── u_sync_src : ip_cdc_sync
├── u_compute : demo_tiny_alu_subsys_compute
│   └── u_alu : demo_tiny_alu
├── u_hs_result : ip_cdc_handshake
├── u_hist_mem : demo_tiny_alu_subsys_mem
└── u_sync_empty : ip_cdc_sync
```

![Block diagram of demo_tiny_alu_subsys_top: the PeakRDL CSR block on the APB clock feeding a handshake and an async FIFO into the ALU datapath on the compute clock, with results returning through a second handshake](../static/img/tour/subsys-block-diagram.png)

Parameter values on each block come from the elaborated design, not from a drawing. `rb hier-query` answers the same questions as JSON (see [Ask the design questions](https://rtl-buddy.github.io/rtl_buddy/v8/tour/#ask-the-design-questions) below), and `rb hub` serves an interactive schematic of the same hierarchy in the browser. See [Hierarchy Rendering](https://rtl-buddy.github.io/rtl_buddy/v8/concepts/hier/).
