## pywellen must stay within 0.25.x

`rb wave` annotations and `rb saif` read traces through pywellen's random-access Waveform API, which pywellen rewrites on every pre-1.0 minor bump. The supported range is therefore two-sided, `>=0.25.6,<0.26`. A forced out-of-range version fails at launch with `pywellen.api_missing` naming the installed version and the supported range; restore the supported dependency range.
