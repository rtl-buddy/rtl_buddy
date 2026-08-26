## Use SDC constraints

A Yosys run extracts `create_clock` periods from the SDC and supplies the shortest period to ABC. It warns when multiple clocks require this approximation.

An OpenROAD run loads the complete SDC and reports actual worst and total negative slack. Use it for multi-clock timing decisions.
