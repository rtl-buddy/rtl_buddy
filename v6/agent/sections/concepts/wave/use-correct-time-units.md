## Use correct time units

Signal reads through pywellen use waveform timescale ticks. Convert them with `Waveform.hierarchy.timescale()`.

Hub and WCP navigation commands use femtoseconds. For example, with a 10 ps waveform tick, 95 ns is 9,500 ticks but 95,000,000 fs. Pass femtoseconds to `rb hub send wave-cursor` and `wave-zoom`.

Surfer command files use waveform ticks, not femtoseconds. Do not pass values between these interfaces without conversion.
