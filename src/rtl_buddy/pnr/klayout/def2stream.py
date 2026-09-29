try:
    import pya
except ImportError:
    pya = None

import fnmatch
import json
import re
import sys
import os


# Version of the JSON result written beside the GDS; `rb pnr` requires an exact
# match and treats any other report as a failed export.
REPORT_SCHEMA = 1


def load_inputs(inputs_json):
    """Read the stream-out input manifest `rb pnr` wrote.

    Returns a dict with the `gds` and `lef` path lists, the `allow_empty` cell
    patterns and the `report` path; missing keys default to empty.

    Free of `pya`, so it is importable outside KLayout.
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

    `patterns` are the run's `gds-allow-empty` cell names or fnmatch globs,
    matched case-sensitively. `allow_empty_regex` is the `GDS_ALLOW_EMPTY`
    environment regex, matched from the start of the name.

    Free of `pya`, so it is importable outside KLayout.
    """
    for pattern in patterns:
        if fnmatch.fnmatchcase(name, pattern):
            return True
    if allow_empty_regex and re.match(allow_empty_regex, name):
        return True
    return False


def classify_empty_cells(names, patterns=(), allow_empty_regex=""):
    """Split empty cells into allowed-empty and missing.

    Returns `(allowed_empty, missing)`, each in the order given. Allowed cells
    are reported but are not errors; missing cells make the export incomplete.

    Free of `pya`, so it is importable outside KLayout.
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
    """Write the JSON stream-out result `rb pnr` reads back.

    Written after the layout, so a report on disk means the GDS was written.

    Free of `pya`, so it is importable outside KLayout.
    """
    if not report_file:
        return
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")


def merge_lef_files(tech_lef_files, extra_lef_files, tech_file=""):
    """Append the run's LEFs to the ones the technology file already names.

    The technology's entries stay first, in order. Entries are de-duplicated on
    the resolved path; a relative path resolves against the `.lyt`'s directory.

    Free of `pya`, so it is importable outside KLayout.
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
        allow_empty: `GDS_ALLOW_EMPTY` regex for cells allowed to be empty.
        lef_files: LEF files the DEF reader needs beyond the technology's own.
        allow_empty_patterns: The run's `gds-allow-empty` names or globs.
        report_file: Where to write the JSON result (empty string for none).

    Returns:
        Number of errors, also the exit code: one per cell with no layout and
        one per orphan cell. Allowed-empty cells are not errors.
    """
    errors = 0

    tech = pya_mod.Technology()
    tech.load(tech_file)
    layout_options = tech.load_layout_options
    if len(layer_map) > 0:
        layout_options.lefdef_config.map_file = layer_map
    if lef_files:
        # Only `lef_files` changes; the other `.lyt` LEF/DEF options stay as set.
        layout_options.lefdef_config.lef_files = merge_lef_files(
            layout_options.lefdef_config.lef_files, lef_files, tech_file
        )

    main_layout = pya_mod.Layout()
    print("[INFO] Reporting cells prior to loading DEF ...")
    for i in main_layout.each_cell():
        print("[INFO] '{0}'".format(i.name))

    main_layout.read(in_def, layout_options)

    top_cell_index = main_layout.cell(design_name).cell_index()

    # Keep VIA_ cells: KLayout names LEF vias VIA_* when reading DEF.
    for i in main_layout.each_cell():
        if i.cell_index() != top_cell_index:
            if not i.name.startswith("VIA_") and not i.name.endswith("_DEF_FILL"):
                i.clear()

    for fil in in_files:
        print("\t{0}".format(fil))
        main_layout.read(fil)

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

    top_only_layout.write(out_file)

    # Last, so a report on disk vouches for the layout.
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
            # Errors other than missing cells.
            "other_errors": errors - len(missing_cells),
            "errors": errors,
        },
    )

    return errors


# Under klayout -r, the -rd flags set the globals tech_file, layer_map, in_def, etc.
if pya is not None:
    try:
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
        # pya is importable but no -rd globals are set.
        pass
