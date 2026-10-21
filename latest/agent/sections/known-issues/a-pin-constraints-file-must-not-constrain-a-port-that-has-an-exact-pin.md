## A `pin-constraints` file must not constrain a port that has an exact pin

rb leaves exact pins out of the `floorplan.pins` side and group rules but does not read the `pin-constraints` file. A `set_io_pin_constraint -region` there that holds an exact pin outside the region fails `place_pins` with `PPL-0107`, reported on another pin. When every signal port has an exact pin, `place_pins` is skipped and the file's constraints have no effect. See [Place IO pins](https://rtl-buddy.github.io/rtl_buddy/v7/concepts/pnr/#place-io-pins).
