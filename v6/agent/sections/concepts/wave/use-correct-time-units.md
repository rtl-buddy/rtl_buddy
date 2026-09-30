## Use correct time units

Three interfaces use three units. Convert between them; never pass a value across unchanged.

- pywellen signal reads use waveform timescale ticks. Convert with `Waveform.timescale`.
- Hub and WCP navigation, including `rb hub send wave-cursor` and `wave-zoom`, use femtoseconds.
- Surfer command files use waveform ticks.

With a 10 ps waveform tick, 95 ns is 9,500 ticks and 95,000,000 fs.
