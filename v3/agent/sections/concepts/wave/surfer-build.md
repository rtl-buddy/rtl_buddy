## Surfer build

The annotation features require Surfer built from the `rtl-buddy` branch:

```bash
git clone https://github.com/rtl-buddy/surfer.git ../surfer
cd ../surfer && git checkout rtl-buddy
cargo build --release
```

Point `cfg-surfer.path` at `../surfer/target/release/surfer` (relative to `root_config.yaml`) or install the binary on `PATH`.
