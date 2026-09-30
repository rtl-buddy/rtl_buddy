## Install Surfer

Mainline Surfer is enough to view FST and VCD files. Live editor annotation needs the `rtl-buddy` branch of the [RTL Buddy Surfer fork](https://github.com/rtl-buddy/surfer/tree/rtl-buddy):

```bash
git clone https://github.com/rtl-buddy/surfer.git ../surfer
cd ../surfer
git checkout rtl-buddy
cargo build --release
```

Put the binary on `PATH` or set its path in `cfg-surfer`.
