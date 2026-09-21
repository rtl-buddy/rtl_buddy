"""cocotb test module — references the shared golden model by name.

Named ``cocotb_*`` rather than ``test_*`` so pytest does not collect this
fixture file. The import is commented out for the same reason: the graph
extractor's reference scan is textual, so the mention is what matters.
"""

# from blk_a_model import expected
