## XPM CDC macros require rtl-buddy-cdc 0.4 or later

rtl-buddy-cdc 0.3.x treats `xpm_cdc_*` instances as dual-clock blackboxes, reports `CDC-BBX`, and drops their crossings from the report and domain map. Upgrade with `uv tool install -U rtl-buddy-cdc`. Waivers can hide the 0.3.x finding but cannot recover crossings beyond the macro.
