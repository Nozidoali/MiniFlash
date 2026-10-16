"""Resynthesis: cut closed windows from a tile program and replace them with smaller cells.

    window    Box, Component, Window, Budget, Slab: what a window is and what it may cost
    extract   stages, close, slabs, extract_greedy, extract_dp: finding the windows and where to cut
    viz       window_box, boxes, lower_cells, write_cells: windows and cells in 3-D
    cell      Cell: a window and the lassynth solution that replaces it

    spec      a window as a lassynth problem: ports, stabilizers, forbidden cubes
    synth     solve, check, minimize: the SAT calls and the depth search

spec and synth need stim, z3, lassynth and kissat, so they are imported by name
(``from miniflash.resynthesis.synth import minimize``) and not from the package.
Putting a synthesised cell back into the program is not here yet.
"""
from .window import LAYERS_PER_STEP, Box, Budget, Component, Slab, Window
from .extract import Weights, close, cost, extract_dp, extract_greedy, skip_cost, slabs, stages, total
from .cell import Cell, CellPipe
from .viz import boxes, lower_cells, timeline, window_box, write_cells

__all__ = ["LAYERS_PER_STEP", "Box", "Budget", "Component", "Slab", "Window", "Weights", "close", "cost", "extract_dp",
           "extract_greedy", "skip_cost", "slabs", "stages", "total", "boxes", "window_box", "Cell", "CellPipe", "lower_cells",
           "timeline", "write_cells"]
