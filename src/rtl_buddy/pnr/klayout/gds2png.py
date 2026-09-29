# Headless GDS -> PNG renderer for KLayout.
#
# Invoked as:
#   klayout -zz -nc \
#     -rd in_gds=... -rd lyp_file=... \
#     -rd out_png=... -rd width=2048 -rd height=2048 \
#     -r tools/openroad/gds2png.py
#
# Inputs are klayout `-rd` globals; `pya` exists only inside klayout.
import pya  # noqa: F401 - provided by klayout -r runtime

lv = pya.LayoutView()
lv.load_layout(in_gds, 0)  # noqa: F821 - set by klayout -rd
if lyp_file:  # noqa: F821
    lv.load_layer_props(lyp_file)  # noqa: F821
lv.max_hier()
lv.zoom_fit()
lv.save_image(out_png, int(width), int(height))  # noqa: F821
