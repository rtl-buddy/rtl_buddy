"""Guard the pywellen API surface rtl_buddy depends on.

pywellen is pre-1.0 and its minor bumps have rewritten the public API: 0.25
removed the entire random-access ``Waveform`` surface that 0.20-0.24 exposed
(see issue #263). The dependency is pinned ``>=0.25.2,<0.26`` so an
API-incompatible bump cannot slip in silently; this test is the other half of
that guard — it asserts at CI time that every attribute ``rb wave``'s value
reader and ``rb saif``'s converter call still exists on the resolved pywellen,
so the next rewrite fails here at lock-bump time instead of in the field.
"""

import pywellen


def test_waveform_random_access_surface():
    # rb wave + rb saif both rely on: path lookup (__getitem__), top-scope
    # enumeration (scopes), and the timescale getter.
    for attr in ("__getitem__", "scopes", "timescale"):
        assert hasattr(pywellen.Waveform, attr), f"pywellen.Waveform.{attr} missing"


def test_signal_point_query_surface():
    # value_at(t) backs wave annotations; slice/len back saif's change scan.
    for attr in ("value_at", "__getitem__", "__len__"):
        assert hasattr(pywellen.Signal, attr), f"pywellen.Signal.{attr} missing"


def test_var_getter_surface():
    # All zero-arg getters since 0.25 (they took a hierarchy arg before).
    for attr in ("signal", "name", "full_name", "var_type", "bitwidth"):
        assert hasattr(pywellen.Var, attr), f"pywellen.Var.{attr} missing"
