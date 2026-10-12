## Look at the waveform

`rb wave` opens the test's trace in [Surfer](https://surfer-project.org/) with the suite's saved signal layout, and streams signal values back into your editor over the hub. The same layout file drives headless capture, which is how this image was made:

```bash
cd verif/demo_tiny_alu
uv run rb wave basic
```

![Surfer view of the basic test: op, a, b, the ALU result y, the four flags, and the scoreboard counters](../static/img/tour/wave-tiny-alu-basic.png)

Stimulus, DUT outputs, and the scoreboard's mismatch counter sit in three named groups because `tb_top.surfer` says so; a new engineer opening the test sees the same thing you do. See [Waveform Viewing](https://rtl-buddy.github.io/rtl_buddy/v6/concepts/wave/).
