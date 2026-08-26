## Install Surfer

Basic FST and VCD viewing works with mainline Surfer. Live editor annotation requires the `rtl-buddy` branch of the [RTL Buddy Surfer fork](https://github.com/rtl-buddy/surfer/tree/rtl-buddy):

```bash
git clone https://github.com/rtl-buddy/surfer.git ../surfer
cd ../surfer
git checkout rtl-buddy
cargo build --release
```

Put the binary on `PATH` or configure its path in `cfg-surfer`.
