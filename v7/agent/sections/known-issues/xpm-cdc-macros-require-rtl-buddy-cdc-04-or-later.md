## XPM CDC macros require rtl-buddy-cdc 0.4 or later

rtl-buddy-cdc 0.3.x treats `xpm_cdc_*` instances as dual-clock blackboxes. It reports `CDC-BBX` and drops their crossings from the report and domain map. Waivers hide the finding but do not recover the crossings. Upgrade with `uv tool install -U rtl-buddy-cdc`.
