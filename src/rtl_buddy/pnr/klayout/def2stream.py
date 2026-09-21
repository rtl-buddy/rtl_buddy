try:
    import pya
except ImportError:
    pya = None

import fnmatch
import json
import re
import sys
import os


# Version of the JSON result this script writes beside the GDS. The reader
# is `rb pnr`, which ships in the same wheel as this script, so it demands
# an exact match: a report it cannot parse — or one an older helper left —
# is a failed export, never a complete one (#619).
REPORT_SCHEMA = 1


def load_inputs(inputs_json):
    """Read the stream-out input manifest `rb pnr` wrote beside this script.

    The inputs travel as JSON rather than as `-rd` strings because a `-rd`
    string has no list contract: splitting one on whitespace silently
    breaks any path containing a space (#617). Returns a dict with the
    `gds` and `lef` path lists, the `allow_empty` cell patterns and the
    `report` path, each defaulted so an older manifest still loads.

    Kept free of `pya` so it is importable — and testable — outside KLayout.
    """
    with open(inputs_json) as f:
        data = json.load(f)
    return {
        "gds": [str(p) for p in data.get("gds", [])],
        "lef": [str(p) for p in data.get("lef", [])],
        "allow_empty": [str(p) for p in data.get("allow_empty", [])],
        "report": str(data.get("report", "")),
    }


def is_allowed_empty(name, patterns=(), allow_empty_regex=""):
    """Whether an empty cell is empty on purpose.

    `patterns` are the run's `gds-allow-empty` entries: a cell name or an
    fnmatch glob over one, matched case-sensitively because GDS cell names
    are. `allow_empty_regex` is the legacy `GDS_ALLOW_EMPTY` environment
    regex, still honoured and still anchored at the start of the name.

    Kept free of `pya` so it is importable — and testable — outside KLayout.
    """
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return True
    if allow_empty_regex and re.match(allow_empty_regex, name):
        return True
    return False


def classify_empty_cells(names, patterns=(), allow_empty_regex=""):
    """Split empty cells into the deliberately abstract and the missing.

    Returns `(allowed_empty, missing)`, each in the order given. A cell the
    allow list covers is a preview macro the user declared — reported, but
    not an error; anything else is a cell whose layout the stream-out could
    not find, which is what makes an export incomplete (#619).

    Kept free of `pya` so it is importable — and testable — outside KLayout.
    """
    allowed_empty = []
    missing = []
    for name in names:
        if is_allowed_empty(name, patterns, allow_empty_regex):
            allowed_empty.append(name)
        else:
            missing.append(name)
    return allowed_empty, missing


def write_report(report_file, report):
    """Write the stream-out result `rb pnr` reads back.

    A file rather than a line for the caller to scrape out of KLayout's
    stdout: cell names are reported verbatim, and a caller that finds no
    report knows the helper did not finish (#619). Written last, after the
    layout, so its presence means the GDS beside it was written too.

    Kept free of `pya` so it is importable — and testable — outside KLayout.
    """
    if not report_file:
        return
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")


def merge_lef_files(tech_lef_files, extra_lef_files, tech_file=""):
    """Append the run's LEFs to the ones the technology file already names.

    The `.lyt` is the PDK's own description of its layers and, usually, of
    its LEFs; replacing that list would strip the masters the existing flow
    relies on, so the technology's entries stay first and in their order and
    the caller's are appended in theirs. Entries are de-duplicated on the
    resolved path, with a relative one taken against the `.lyt`'s directory,
    which is where KLayout itself reads it from.

    Kept free of `pya` so it is importable — and testable — outside KLayout.
    """
    base = os.path.dirname(tech_file)

    def _key(path):
        return os.path.normcase(os.path.realpath(os.path.join(base, path)))

    merged = list(tech_lef_files)
    seen = {_key(p) for p in merged}
    for path in extra_lef_files:
        key = _key(path)
        if key in seen:
            continue
        seen.add(key)
        merged.append(path)
    return merged


def merge_gds(
    pya_mod,
    tech_file,
    layer_map,
    in_def,
    design_name,
    in_files,
    seal_file,
    out_file,
    allow_empty="",
    lef_files=(),
    allow_empty_patterns=(),
    report_file="",
):
    """Merge DEF and GDS/OAS files into a single stream file.

    Args:
        pya_mod: The pya module (klayout Python API).
        tech_file: Path to klayout technology file.
        layer_map: Path to layer map file (empty string if none).
        in_def: Path to input DEF file.
        design_name: Top-level design name.
        in_files: List of GDS/OAS files to merge.
        seal_file: Path to seal ring GDS/OAS file (empty string if none).
        out_file: Path to output GDS/OAS file.
        allow_empty: Legacy `GDS_ALLOW_EMPTY` regex for cells allowed to be
            empty.
        lef_files: LEF files the DEF reader needs on top of the technology's
            own, in reader order (technology LEF, PDK macro LEF, then the
            run's macro LEFs).
        allow_empty_patterns: The run's `gds-allow-empty` cell names or
            globs — the per-run form of `allow_empty`.
        report_file: Where to write the JSON result the caller reads back
            (empty string to write none).

    Returns:
        Number of errors encountered, which is also the exit code: one per
        cell with no layout and one per orphan cell. A cell the allow list
        covers is reported and is not an error.
    """
    errors = 0

    # Load technology file
    tech = pya_mod.Technology()
    tech.load(tech_file)
    layout_options = tech.load_layout_options
    if len(layer_map) > 0:
        layout_options.lefdef_config.map_file = layer_map
    if lef_files:
        # Only `lef_files` is touched: `read_lef_with_def`,
        # `macro_resolution_mode` and the rest stay as the `.lyt` set them,
        # so a PDK that already streams correctly keeps doing so and only
        # gains the masters it was missing.
        layout_options.lefdef_config.lef_files = merge_lef_files(
            layout_options.lefdef_config.lef_files, lef_files, tech_file
        )

    # Load def file
    main_layout = pya_mod.Layout()
    print("[INFO] Reporting cells prior to loading DEF ...")
    for i in main_layout.each_cell():
        print("[INFO] '{0}'".format(i.name))

    main_layout.read(in_def, layout_options)

    # Clear cells
    top_cell_index = main_layout.cell(design_name).cell_index()

    # remove orphan cell BUT preserve cell with VIA_
    #  - KLayout is prepending VIA_ when reading DEF that instantiates LEF's via
    for i in main_layout.each_cell():
        if i.cell_index() != top_cell_index:
            if not i.name.startswith("VIA_") and not i.name.endswith("_DEF_FILL"):
                i.clear()

    # Load in the gds to merge
    for fil in in_files:
        print("\t{0}".format(fil))
        main_layout.read(fil)

    # Copy the top level only to a new layout
    top_only_layout = pya_mod.Layout()
    top_only_layout.dbu = main_layout.dbu
    top = top_only_layout.create_cell(design_name)
    top.copy_tree(main_layout.cell(design_name))

    if allow_empty:
        print("[INFO] GDS_ALLOW_EMPTY={0}".format(allow_empty))
    if allow_empty_patterns:
        print("[INFO] gds-allow-empty={0}".format(" ".join(allow_empty_patterns)))

    empty_cells = [i.name for i in top_only_layout.each_cell() if i.is_empty()]
    allowed_empty, missing_cells = classify_empty_cells(
        empty_cells, allow_empty_patterns, allow_empty
    )
    for name in allowed_empty:
        print("[WARNING] LEF Cell '{0}' ignored. Allowed to be empty.".format(name))
    for name in missing_cells:
        print(
            "[ERROR] LEF Cell '{0}' has no matching GDS/OAS cell."
            " Cell will be empty.".format(name)
        )
    errors += len(missing_cells)

    if not empty_cells:
        print("[INFO] All LEF cells have matching GDS/OAS cells")

    orphan_cells = []
    for i in top_only_layout.each_cell():
        if i.name != design_name and i.parent_cells() == 0:
            orphan_cells.append(i.name)
            print("[ERROR] Found orphan cell '{0}'".format(i.name))
    errors += len(orphan_cells)

    if not orphan_cells:
        print("[INFO] No orphan cells in the final layout")

    if seal_file:
        top_cell = top_only_layout.top_cell()

        top_only_layout.read(seal_file)

        for cell in top_only_layout.top_cells():
            if cell != top_cell:
                print(
                    "[INFO] Merging '{0}' as child of '{1}'".format(
                        cell.name, top_cell.name
                    )
                )
                top.insert(pya_mod.CellInstArray(cell.cell_index(), pya_mod.Trans()))

    # Write out the GDS
    top_only_layout.write(out_file)

    # Last, so that a report on disk vouches for the layout beside it: a
    # caller that reads one knows this script got all the way here.
    write_report(
        report_file,
        {
            "schema": REPORT_SCHEMA,
            "design": design_name,
            "out_file": out_file,
            "complete": not missing_cells,
            "missing_cells": missing_cells,
            "allowed_empty_cells": allowed_empty,
            "orphan_cells": orphan_cells,
            # Errors the missing cells do not account for, so the caller can
            # tell "a preview macro has no layout" from "the stream-out went
            # wrong in some other way" without parsing this script's stdout.
            "other_errors": errors - len(missing_cells),
            "errors": errors,
        },
    )

    return errors


# When run via klayout -r, globals tech_file, layer_map, in_def, etc.
# are set by klayout's -rd mechanism.
if pya is not None:
    try:
        # These globals are set by klayout -rd flags
        manifest = load_inputs(inputs_json)  # noqa: F821
        sys.exit(
            merge_gds(
                pya_mod=pya,
                tech_file=tech_file,  # noqa: F821 - set by klayout -rd
                layer_map=layer_map,  # noqa: F821
                in_def=in_def,  # noqa: F821
                design_name=design_name,  # noqa: F821
                in_files=manifest["gds"],
                seal_file=seal_file,  # noqa: F821
                out_file=out_file,  # noqa: F821
                allow_empty=os.environ.get("GDS_ALLOW_EMPTY", ""),
                lef_files=manifest["lef"],
                allow_empty_patterns=manifest["allow_empty"],
                report_file=manifest["report"],
            )
        )
    except NameError:
        # Not running under klayout -r, pya available but no -rd globals
        pass
