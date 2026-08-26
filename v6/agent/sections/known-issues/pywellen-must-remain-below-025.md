## pywellen must remain below 0.25

`rb wave` annotations and `rb saif` require pywellen's removed random-access API, so the supported range is `>=0.20,<0.25`. A forced newer version fails at launch with `pywellen.api_missing`; restore the supported dependency range.
