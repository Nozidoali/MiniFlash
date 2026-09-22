"""Spacing: how much of the array a program keeps, and when.

`space(problem, mapping, schedule, mode)` builds the program from a routed schedule.
mode "none" keeps the whole array. mode "whole" compacts the whole schedule once.
mode "partition" is "+ Spacing": the schedule is cut into partitions,
each compacted on its own, with walks between them.

## Compaction

A row is load-bearing when it holds a qubit tile, a magic tile some route consumes,
or a segment endpoint (route starts, ends, and L corners). A row that vertical
segments merely cross is not: removing it shortens those segments by one tile and
nothing else changes. Columns likewise with horizontal segments. Removing a
load-free row keeps every route straight, chained, disjoint, and adjacent to its
parties, so the compacted schedule passes the same checks with a smaller grid.

Magic tiles no route consumes are dropped from the mapping as well: the compacted
program lists exactly the magic supply the schedule uses.

`compact` is applied between routing and the program dump, to the whole schedule
or to each partition of it (staged.py).

## Staging

The mapping and the routed schedule come from the earlier stages. The routed
steps are cut greedily into partitions: a partition accumulates the rows and
columns its steps load, and closes when the next step would push either count past
its budget. Each partition is compacted separately (see compact.py), so the qubit
order is shared but every partition has its own lane vectors and mapping.

The chip is the largest partition height by the largest width; every partition is
anchored at the top-left. Between two partitions each qubit walks from its old tile
to its new one, one tile per axis per step, so the boundary costs the maximum
Chebyshev displacement in bubble steps that carry only qubit vertices. Both mappings
realize the same order, so the walk never makes two qubits collide.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from ..circuit import Problem
from ..mapping import Coord, Layout, Mapping
from ..program import QUBIT, Program, Step
from ..route import Route, Schedule, Segment

MODES = ("none", "whole", "partition")


def space(problem: Problem, mapping: Mapping, schedule: Schedule, mode: str = "none",
          windows: Optional[List[Tuple[int, int]]] = None, budgets: Optional[Tuple[int, int]] = None) -> Tuple[Program, list]:
    """The program and, for mode "partition", its partitions. Windows come from the router
    when given; otherwise `cut` closes them by the (row, column) `budgets`."""
    if mode == "none":
        return Program.from_routes(problem, mapping, schedule), []
    if mode == "whole":
        mapping, schedule, keep_rows, keep_cols = compact(mapping, schedule)
        return Program.from_routes(problem, mapping, schedule), [keep_rows, keep_cols]
    if mode != "partition":
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    if windows is None:
        windows = cut(mapping, schedule, *budgets)
    return stage(problem, mapping, schedule, windows)


def load_bearing(mapping: Mapping, schedule: Schedule) -> Tuple[Set[int], Set[int]]:
    """Rows and columns that must survive compaction."""
    rows: Set[int] = set()
    cols: Set[int] = set()

    def keep(t: Coord):
        rows.add(t[0])
        cols.add(t[1])

    for t in mapping.qubits.values():
        keep(t)
    for step in schedule:
        for r in step:
            if r.magic is not None:
                keep(r.magic)
            for s in r.segments:
                keep(s.start)
                keep(s.end)
    return rows, cols


def compact(mapping: Mapping, schedule: Schedule) -> Tuple[Mapping, Schedule, List[int], List[int]]:
    """Return the compacted mapping and schedule plus the kept original rows and columns."""
    rows, cols = load_bearing(mapping, schedule)
    keep_rows = sorted(rows)
    keep_cols = sorted(cols)
    row_index = {row: i for i, row in enumerate(keep_rows)}
    col_index = {col: i for i, col in enumerate(keep_cols)}

    def remap(tile: Coord) -> Coord:
        return row_index[tile[0]], col_index[tile[1]]

    consumed = {route.magic for step in schedule for route in step if route.magic is not None}
    new_mapping = Mapping(
        len(keep_rows),
        len(keep_cols),
        {q: remap(tile) for q, tile in mapping.qubits.items()},
        tuple(remap(tile) for tile in mapping.magic if tile in consumed),
    )
    new_schedule: Schedule = []
    for step in schedule:
        routes = []
        for route in step:
            segments = tuple(Segment(remap(s.start), remap(s.end)) for s in route.segments)
            magic = None if route.magic is None else remap(route.magic)
            routes.append(Route(route.pair, segments, magic))
        new_schedule.append(routes)
    return new_mapping, new_schedule, keep_rows, keep_cols


@dataclass(frozen=True)
class Partition:
    start: int                 # first routed step (inclusive)
    stop: int                  # last routed step (exclusive)
    mapping: Mapping           # compacted, anchored at the top-left
    schedule: Schedule         # the partition's routes on the compacted mapping
    keep_rows: Tuple[int, ...]  # original rows kept
    keep_cols: Tuple[int, ...]


def cut(mapping: Mapping, schedule: Schedule, row_budget: int, col_budget: int) -> List[Tuple[int, int]]:
    """Greedy step windows [start, stop) whose load-bearing rows/cols stay within budget."""
    windows = []
    start = 0
    rows, cols = set(), set()
    for t, step in enumerate(schedule):
        r, c = load_bearing(mapping, [step])
        if t > start and (len(rows | r) > row_budget or len(cols | c) > col_budget):
            windows.append((start, t))
            start, rows, cols = t, set(), set()
        rows |= r
        cols |= c
    if start < len(schedule):
        windows.append((start, len(schedule)))
    return windows


def walk_steps(before: Mapping, after: Mapping) -> List[Step]:
    """Move one tile per axis per step between order-preserving compacted mappings.

    Rows and columns move simultaneously. The slowest qubit sets the duration;
    qubits that arrive sooner wait at their destination. Arbitrary permutations
    are not supported: collision freedom relies on preserving the site order.
    """
    displacement = {
        q: (after.qubits[q][0] - row, after.qubits[q][1] - col)
        for q, (row, col) in before.qubits.items()
    }
    duration = max((max(abs(dr), abs(dc)) for dr, dc in displacement.values()), default=0)
    steps = []
    for elapsed in range(1, duration + 1):
        step = Step()
        for q, (row, col) in before.qubits.items():
            dr, dc = displacement[q]
            tile = (
                row + max(-elapsed, min(elapsed, dr)),
                col + max(-elapsed, min(elapsed, dc)),
            )
            step.add(tile, QUBIT, q)
        steps.append(step)
    return steps


def budgets(layout: Layout, extra: int = 6, row_budget: Optional[int] = None, col_budget: Optional[int] = None) -> Tuple[int, int]:
    """Counts of load-bearing rows and columns a partition may retain, including the
    site rows and columns themselves: site_rows + extra and site_cols + extra unless
    an explicit budget overrides that axis."""
    return (row_budget if row_budget is not None else layout.site_rows + extra,
            col_budget if col_budget is not None else layout.site_cols + extra)


def stage(problem: Problem, mapping: Mapping, schedule: Schedule,
          windows: List[Tuple[int, int]]) -> Tuple[Program, List[Partition]]:
    """Compact each window on its own, anchor every partition at the top-left of the
    largest one, and walk the qubits between consecutive partitions."""
    partitions = []
    for start, stop in windows:
        m, part, kr, kc = compact(mapping, schedule[start:stop])
        partitions.append(Partition(start, stop, m, part, tuple(kr), tuple(kc)))
    height = max(p.mapping.height for p in partitions)
    width = max(p.mapping.width for p in partitions)
    prog = Program(height, width)
    previous: Optional[Mapping] = None
    for p in partitions:
        chip = Mapping(height, width, p.mapping.qubits, p.mapping.magic)
        if previous is not None:
            prog.steps.extend(walk_steps(previous, chip))
        prog.steps.extend(Program.from_routes(problem, chip, p.schedule).steps)
        previous = chip
    return prog, partitions
