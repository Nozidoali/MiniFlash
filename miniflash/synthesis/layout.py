"""Layout selection: choose the site array's shape ("+ Layout").

`candidate_shapes` lists every a x b site array with a <= b that holds the qubits,
ordered by the paired-lane grid area; Compiler(layout=N) tries the first N of them
(main's rule) plus the squarest feasible shape if it is not among them: the
budgeted router wants routing space, and the flat shapes that minimise area are
the ones it handles worst. Each shape is compiled from scratch and the cheapest
program is kept; `tried` records every candidate. Magic supply always sits at both
row ends. The statistics policy (Compiler(layout="stats")) uses `shape_estimates`
to price full grid area and layer capacity, then compiles only its preferred
shape. Predictions do not include compaction or walks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ..circuit import Problem
from ..mapping import Mapping

Shape = Tuple[int, int]


def candidate_shapes(n_qubits: int, count: Optional[int] = None, squarest: bool = False) -> List[Shape]:
    """(site_rows, site_cols) with rows <= cols, smallest paired-lane grid area first.
    With `squarest`, the shape with the smallest cols - rows is appended if the first
    `count` do not already contain it."""
    cands = []
    for a in range(1, n_qubits + 1):
        b = -(-n_qubits // a)
        if a > b:
            break
        m = Mapping.paired(a, b, n_qubits)
        cands.append((m.height * m.width, a, b))
    cands.sort()
    out = [(a, b) for _, a, b in cands]
    if count is None:
        return out
    chosen = out[:count]
    if squarest:
        sq = min(out, key=lambda ab: (ab[1] - ab[0], ab))
        if sq not in chosen:
            chosen.append(sq)
    return chosen


@dataclass(frozen=True)
class ShapeEstimate:
    """A shape-model prediction, not a routed or verified program."""

    shape: Shape
    area: int
    steps: int

    @property
    def volume(self) -> int:
        return self.area * self.steps


def shape_estimates(problem: Problem, cap: float = 1.0) -> List[ShapeEstimate]:
    """Price each row count by full paired-grid area times estimated layer duration.

    A layer needs at least one step, with capacity of two T gates and ``cap`` CX
    pairs per site row. The model ignores actual routes, compaction, and walks.
    Row counts range from one through ceil(sqrt(n_qubits)).
    """
    demand = []
    for layer in problem.layers():
        magic_pairs = sum(problem.pairs[i].is_magic for i in layer)
        demand.append((magic_pairs, len(layer) - magic_pairs))

    estimates = []
    for rows in range(1, math.ceil(math.sqrt(problem.n_qubits)) + 1):
        cols = -(-problem.n_qubits // rows)
        mapping = Mapping.paired(rows, cols, problem.n_qubits)
        steps = sum(
            max(1, -(-magic_pairs // (2 * rows)), math.ceil(cx_pairs / (cap * rows)))
            for magic_pairs, cx_pairs in demand
        ) or 1
        estimates.append(ShapeEstimate((rows, cols), mapping.height * mapping.width, steps))
    return estimates


def stats_shape(problem: Problem, cap: float = 1.0) -> Shape:
    """Choose the smallest predicted volume; ties prefer fewer site rows."""
    return min(shape_estimates(problem, cap), key=lambda estimate: (estimate.volume, estimate.shape[0])).shape
