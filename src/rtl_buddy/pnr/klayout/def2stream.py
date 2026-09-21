try:
    import pya
except ImportError:
    pya = None

import json
import re
import sys
import os


def load_inputs(inputs_json):
    """Read the stream-out input manifest `rb pnr` wrote beside this script.

    The GDS and LEF lists travel as JSON rather than as one `-rd` string
    because a `-rd` string has no path-list contract: splitting it on
    whitespace silently breaks any path containing a space (#617). Returns
    `(gds_files, lef_files)`; either may be empty.

    Kept free of `pya` so it is importable — and testable — outside KLayout.
    """
    with open(inputs_json) as f:
        data = json.load(f)
    return (
        [str(p) for p in data.get("gds", [])],
        [str(p) for p in data.get("lef", [])],
    )


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
        allow_empty: Regex pattern for cells allowed to be empty.
        lef_files: LEF files the DEF reader needs on top of the technology's
            own, in reader order (technology LEF, PDK macro LEF, then the
            run's macro LEFs).

    Returns:
        Number of errors encountered.
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

    missing_cell = False
    regex = re.compile(allow_empty) if allow_empty else None

    if allow_empty:
        print(f"[INFO] GDS_ALLOW_EMPTY={allow_empty}")

    for i in top_only_layout.each_cell():
        if i.is_empty():
            missing_cell = True
            if regex is not None and regex.match(i.name):
                print(
                    "[WARNING] LEF Cell '{0}' ignored. Matches GDS_ALLOW_EMPTY.".format(
                        i.name
                    )
                )
            else:
                print(
                    "[ERROR] LEF Cell '{0}' has no matching GDS/OAS cell."
                    " Cell will be empty.".format(i.name)
                )
                errors += 1

    if not missing_cell:
        print("[INFO] All LEF cells have matching GDS/OAS cells")

    orphan_cell = False
    for i in top_only_layout.each_cell():
        if i.name != design_name and i.parent_cells() == 0:
            orphan_cell = True
            print("[ERROR] Found orphan cell '{0}'".format(i.name))
            errors += 1

    if not orphan_cell:
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

    return errors


# When run via klayout -r, globals tech_file, layer_map, in_def, etc.
# are set by klayout's -rd mechanism.
if pya is not None:
    try:
        # These globals are set by klayout -rd flags
        gds_files, extra_lefs = load_inputs(inputs_json)  # noqa: F821
        sys.exit(
            merge_gds(
                pya_mod=pya,
                tech_file=tech_file,  # noqa: F821 - set by klayout -rd
                layer_map=layer_map,  # noqa: F821
                in_def=in_def,  # noqa: F821
                design_name=design_name,  # noqa: F821
                in_files=gds_files,
                seal_file=seal_file,  # noqa: F821
                out_file=out_file,  # noqa: F821
                allow_empty=os.environ.get("GDS_ALLOW_EMPTY", ""),
                lef_files=extra_lefs,
            )
        )
    except NameError:
        # Not running under klayout -r, pya available but no -rd globals
        pass
