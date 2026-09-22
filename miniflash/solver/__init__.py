"""The compiler's passes, one module per component of the compiler's ladder.

    placement.place     where each qubit sits: index order, or spectral seed and anneal
    schedule.schedule   which pairs execute per step: one, or every ready pair (dependency)
    routing.route       the criticality-driven router with lane budgets (routing, spacing)
    spacing.space       how much array a program keeps: all, compacted once, or partitions with walks
    layout              candidate shapes and the statistics prediction (layout selection)

miniflash.compiler.Compiler composes them.
"""
from .layout import Shape, candidate_shapes, shape_estimates, stats_shape
from .placement import Placement, place, square_shape
from .routing import route
from .schedule import schedule
from .spacing import MODES, Partition, budgets, compact, cut, space, stage, walk_steps

__all__ = ["Shape", "candidate_shapes", "shape_estimates", "stats_shape", "Placement", "place", "square_shape",
           "route", "schedule", "MODES", "Partition", "budgets", "compact", "cut", "space", "stage", "walk_steps"]
