"""Stand-ins for the platform and PDK an `rb power` backend reads, for tests that generate its script."""


class _FakePdk:
    """A `cfg-pdks` corner as the power script reads it: two LEF paths."""

    def __init__(
        self, tech_lef, macro_lef=None, fill_cells=(), layer_rc_tcl="", platform_tcl=""
    ):
        self._tech_lef = tech_lef
        self._macro_lef = macro_lef
        self._layer_rc_tcl = layer_rc_tcl
        self._platform_tcl = platform_tcl
        # `filler_placement` adds tens of thousands of these to a routed database; they
        # have no Liberty and no power.
        self._fill_cells = list(fill_cells)

    def get_name(self):
        return "fake_pdk"

    def get_fill_cells(self):
        return list(self._fill_cells)

    def get_tech_lef(self):
        return self._tech_lef

    def get_macro_lef_paths(self):
        if isinstance(self._macro_lef, list):
            return list(self._macro_lef)
        return [self._macro_lef] if self._macro_lef else []

    def get_layer_rc_tcl(self):
        return self._layer_rc_tcl

    def get_platform_tcl(self):
        return self._platform_tcl


class _FakePlatform:
    """A `cfg-pnr-platforms` entry: one Liberty, over one PDK corner.

    Named paths rather than a `MagicMock`, because the power fingerprint digests the
    technology and a mock answers every call with a new object.
    """

    def __init__(
        self,
        liberty="/pdk/fake/nangate45_typ.lib",
        pdk=None,
        corners=None,
        max_fanout=None,
    ):
        self._liberty = liberty
        self._max_fanout = max_fanout
        self._pdk = pdk or _FakePdk("/pdk/fake/tech.lef")
        # corner -> Liberty, primary first, for a multi-corner platform; `None` is the
        # single-corner platform other tests use.
        self._corners = corners

    def get_sta_lib_paths(self):
        return [self._liberty]

    def is_multi_corner(self):
        return bool(self._corners) and len(self._corners) > 1

    def get_sta_corners(self):
        return list(self._corners) if self._corners else ["typ"]

    def get_sta_corner_lib_paths(self):
        return {c: [lib] for c, lib in (self._corners or {}).items()}

    def get_pdk(self):
        return self._pdk

    def get_max_fanout(self):
        return self._max_fanout
