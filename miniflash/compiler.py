"""Compiler: one class, configured by a ladder of components added one at a time.

    Compiler(dependency=False)                   # vanilla: one pair per step
    Compiler()                                   # + dependency-aware scheduling
    Compiler(routing=True)                       # + criticality-driven routing on the fixed grid
    Compiler(routing=True, spacing=True)         # + on-demand spacing: partitions, compaction, walks
    Compiler(..., placement=True)                # + spectral placement and annealing on the paired-lane array
    Compiler(..., layout="stats")                # + layout selection: the shape predicted from the DAG
    Compiler(..., layout=3)                      # or compare the three smallest-area shapes

Every configuration runs the same three passes, each a function in miniflash/solver:
`placement.place` (index order or placed), `schedule.schedule` or `routing.route`
(the schedule, and for the router the partition windows), and `spacing.space` (a
plain program, one compaction, or partitions with walks). `layout` repeats them per
candidate shape (`layout.candidate_shapes`, `layout.stats_shape`) and keeps the
lowest final volume. The passes are timed into `profile`, one entry per tried
shape, for the saved-program analyses.

Two development controls fall outside the ladder: `spacing="whole"`
compacts the whole schedule once without partitions or walks, and `spacing=True`
without `routing` cuts a finished ASAP schedule by budget.
"""
from __future__ import annotations

import functools
import time
from typing import List, Optional, Tuple, Union

from .solver import routing
from .circuit import Problem
from .mapping import Layout, Mapping
from .solver.layout import Shape, candidate_shapes, stats_shape
from .solver.placement import COLL, LANE, LEN, Placement, place
from .factory import Factory, supply_limits
from .program import Program
from .route import Schedule
from .solver.schedule import schedule
from .solver.spacing import Partition, budgets, space

LADDER = ("vanilla", "dependency", "routing", "spacing", "placement", "layout")
DEFAULTS = dict(extra=6, share=4.0, seed=0)


class Compiler:
    def __init__(self, dependency: bool = True, routing: bool = False, spacing: Union[bool, str] = False,
                 placement: Union[bool, Placement] = False, layout: Union[None, str, int] = None,
                 shape: Optional[Shape] = None,
                 extra: int = 6, share: float = 4.0, row_budget: Optional[int] = None, col_budget: Optional[int] = None,
                 exact: int = 12, factory: Optional[Factory] = None, factories: int = 0,
                 seed: int = 0, iters: Optional[int] = None, anneal: bool = True,
                 weights: Tuple[float, float, float] = (COLL, LANE, LEN)):
        """dependency  False: one pair per step. True: ASAP scheduling. Ignored once `routing` is on.
        routing     the criticality-driven router with lane scoring; without `spacing` its budget
                    is the whole grid, so the fixed array is kept and no walks appear.
        spacing     True: partition by budget, compact each partition, walk between (on-demand
                    spacing; with `routing` the router closes the partitions, without it
                    the finished schedule is cut). "whole": compact the whole program once.
        placement   True: spectral placement and annealing on the paired-lane array. A `Placement`
                    fixes the mapping and layout instead. False: index order on the sparse square.
        layout      None: the array of `shape` (the smallest square by default). "stats": the one
                    shape predicted from the DAG. An int: compare that many smallest-area shapes.
        factory     None: unlimited magic supply. A Factory(H, W, T) alone: one behind every magic
                    tile, each tile yielding a state every T steps.
        factories   F > 0: F shared factories (the default Factory() if none is given) feed the
                    whole chip from step 0, F states per T steps (see factory.supply_limits).
        extra, share, row_budget, col_budget, exact configure the budgets and the router;
        seed, iters, anneal, weights configure placement."""
        if spacing not in (False, True, "whole"):
            raise ValueError(f"spacing must be False, True, or 'whole', not {spacing!r}")
        if layout is not None and not (isinstance(layout, int) or layout == "stats"):
            raise ValueError(f"layout must be None, 'stats', or a count, not {layout!r}")
        if isinstance(placement, Placement) and layout is not None:
            raise ValueError("a fixed placement has one shape; layout selection needs placement=True")
        supply_limits(factory, factories)   # validates
        self.dependency, self.routing, self.spacing, self.placement, self.layout_mode = dependency, routing, spacing, placement, layout
        self.shape_option = shape
        self.extra, self.share, self.row_budget, self.col_budget, self.exact = extra, share, row_budget, col_budget, exact
        self.factory, self.factories = factory, factories
        self.seed, self.iters, self.anneal, self.weights = seed, iters, anneal, weights
        # results of the last solve (of the selected shape when several were tried)
        self.shape: Optional[Shape] = None
        self.layout: Optional[Layout] = None
        self.mapping: Optional[Mapping] = None
        self.partitions: List[Partition] = []
        self.windows: List[Tuple[int, int]] = []
        self.keep_rows: List[int] = []
        self.keep_cols: List[int] = []
        self.initial_cost = self.final_cost = 0.0
        self.tried: List[dict] = []
        self.profile: List[dict] = []

    @property
    def name(self) -> str:
        """The ladder label of the last component, with non-default options as suffixes."""
        if self.layout_mode is not None:
            label = "layout"
        elif self.placement:
            label = "placement" if self.spacing is True and self.routing else "placed"
        elif self.spacing == "whole":
            label = "compact"
        elif self.spacing:
            label = "spacing" if self.routing else "staged"
        elif self.routing:
            label = "routing"
        else:
            label = "dependency" if self.dependency else "vanilla"
        suffix = ""
        if isinstance(self.layout_mode, int):
            suffix += f"-shapes{self.layout_mode}"
        if self.seed != DEFAULTS["seed"] and self.placement:
            suffix += f"-s{self.seed}"
        if self.factory is not None and self.routing:
            suffix += f"-{self.factory.height}x{self.factory.width}x{self.factory.period}"
        if self.factories and self.routing:
            suffix += f"-f{self.factories}"
        if self.extra != DEFAULTS["extra"] and self.spacing is True:
            suffix += f"-extra{self.extra}"
        if self.share != DEFAULTS["share"] and self.routing:
            suffix += f"-share{self.share:g}"
        return label + suffix

    # ---- the three passes ------------------------------------------------------------
    def map(self, problem: Problem, shape: Optional[Shape] = None) -> Mapping:
        """Mapping and layout for one shape: fixed, placed, or index order."""
        if isinstance(self.placement, Placement):
            placed = self.placement
        else:
            placed = place(problem, shape, bool(self.placement), self.seed, self.iters, self.anneal, self.weights)
        self.mapping, self.layout = placed.mapping, placed.layout
        self.initial_cost, self.final_cost = placed.initial_cost, placed.final_cost
        return placed.mapping

    def budgets(self, mapping: Mapping) -> Tuple[int, int]:
        """Rows and columns a partition may retain; the whole grid when nothing is partitioned."""
        if self.spacing is True:
            return budgets(self.layout, self.extra, self.row_budget, self.col_budget)
        return (self.row_budget if self.row_budget is not None else mapping.height,
                self.col_budget if self.col_budget is not None else mapping.width)

    def route(self, problem: Problem, mapping: Mapping) -> Schedule:
        """The schedule; the router also records the partition windows it closed."""
        self.windows = []
        if self.routing:
            steps, self.windows = routing.route(problem, mapping, *self.budgets(mapping), self.share, self.exact,
                                                *supply_limits(self.factory, self.factories))
            return steps
        return schedule(problem, mapping, self.dependency)

    def build(self, problem: Problem, mapping: Mapping, steps: Schedule) -> Program:
        """Plain program, whole-program compaction, or staged partitions with walks."""
        self.partitions, self.keep_rows, self.keep_cols = [], [], []
        if self.spacing is True:
            program, self.partitions = space(problem, mapping, steps, "partition",
                                             windows=self.windows if self.routing else None, budgets=self.budgets(mapping))
        elif self.spacing == "whole":
            program, (self.keep_rows, self.keep_cols) = space(problem, mapping, steps, "whole")
        else:
            program, _ = space(problem, mapping, steps)
        return program

    def solve_shape(self, problem: Problem, shape: Optional[Shape] = None) -> Program:
        """One compilation on one shape, timed into `profile`."""
        t0 = time.perf_counter()
        mapping = self.map(problem, shape)
        t1 = time.perf_counter()
        steps = self.route(problem, mapping)
        t2 = time.perf_counter()
        program = self.build(problem, mapping, steps)
        t3 = time.perf_counter()
        self.shape = shape if shape is not None else (self.layout.site_rows, self.layout.site_cols)
        self.profile.append({"map_s": t1 - t0, "route_s": t2 - t1, "windows_s": 0.0, "compact_s": t3 - t2, "total_s": t3 - t0,
                             "shape": list(self.shape), "grid": [program.height, program.width],
                             "steps": len(program.steps), "volume": program.volume})
        return program

    def solve(self, problem: Problem) -> Program:
        self.tried, self.profile = [], []
        if self.layout_mode is None:
            return self.solve_shape(problem, self.shape_option)
        shapes = [stats_shape(problem)] if self.layout_mode == "stats" else candidate_shapes(problem.n_qubits, self.layout_mode, squarest=True)
        best = None
        for shape in shapes:
            program = self.solve_shape(problem, shape)
            self.tried.append({"shape": shape, "grid": (program.height, program.width), "steps": len(program), "volume": program.volume})
            if best is None or program.volume < best[0].volume:
                best = (program, shape, self.layout, self.mapping, self.partitions, self.windows,
                        self.keep_rows, self.keep_cols, self.initial_cost, self.final_cost)
        (program, self.shape, self.layout, self.mapping, self.partitions, self.windows,
         self.keep_rows, self.keep_cols, self.initial_cost, self.final_cost) = best
        return program


PRESETS = {  # name -> Compiler keyword arguments; the ladder, then the development controls
    "vanilla": dict(dependency=False),
    "dependency": dict(),
    "routing": dict(routing=True),
    "spacing": dict(routing=True, spacing=True),
    "placement": dict(routing=True, spacing=True, placement=True),
    "layout": dict(routing=True, spacing=True, placement=True, layout="stats"),
    "compact": dict(spacing="whole"),
    "staged": dict(spacing=True),
    "placed": dict(placement=True),
}
def make(name: str, shapes: Union[None, str, int] = None, **options) -> Compiler:
    """A preset by name with option overrides. `shapes` replaces the
    preset's layout mode: "stats" or a count; "default" and None keep it."""
    if name not in PRESETS:
        raise KeyError(f"unknown solver {name!r}; choose from {', '.join(PRESETS)}")
    kw = dict(PRESETS[name])
    if shapes not in (None, "default") and "layout" in kw:
        kw["layout"] = shapes
    kw.update(options)
    return Compiler(**kw)


SOLVERS = {name: functools.partial(make, name) for name in PRESETS}
