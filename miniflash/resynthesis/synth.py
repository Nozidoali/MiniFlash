"""Synth: run lassynth on a window's spec, and press its depth.

    solve      one SAT call through kissat under a wall-clock budget: sat, unsat or timeout;
               answers are kept on disk, keyed by the spec
    check      the spec with the tile program's own geometry plugged in; sat means the
               spec and the tile model agree
    minimize   from one layer upwards, one call per depth, until a depth is satisfiable:
               unsatisfiable depths answer fast, so this is the cheap direction
    cell       the shallowest cell found for a window, or the tile program's own geometry
               as a cell when nothing shallower was found in time
    cells      one cell per window of every slab

Needs lassynth (scripts/download_lassynth.py), z3, stim and a kissat binary; none of
them is imported until a function here runs.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ..circuit import Problem
from ..program import Program
from .cell import Cell
from .spec import geometry, layers, spec
from .window import Slab, Window

SAT, UNSAT, TIMEOUT = "sat", "unsat", "timeout"
CACHE = Path(os.environ.get("MINIFLASH_CACHE", ".miniflash-cache")) / "resynthesis.v1"


@dataclass(frozen=True)
class Result:
    status: str
    depth: int          # layers asked for, the port layer not counted
    seconds: float
    lasre: Optional[dict] = None

    def __str__(self) -> str:
        return f"depth {self.depth}: {self.status} in {self.seconds:.2f}s"


def kissat_binary() -> str:
    """The kissat executable: $MINIFLASH_KISSAT_BIN, the one beside the interpreter, or
    the first on the path."""
    for cand in (os.environ.get("MINIFLASH_KISSAT_BIN"), str(Path(sys.executable).parent / "kissat"), shutil.which("kissat")):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise FileNotFoundError("no kissat binary: set MINIFLASH_KISSAT_BIN")


def _key(specification: dict, given_vals: Optional[List[dict]]) -> str:
    body = json.dumps({"spec": specification, "given": given_vals}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def solve(specification: dict, given_vals: Optional[List[dict]] = None, timeout: int = 60, cache: bool = True) -> Result:
    """One lassynth call. kissat stops itself after `timeout` seconds; lassynth reports
    that as an unknown return code, which is the timeout here. A cached sat or unsat
    answer is final; a cached timeout only stands for budgets no larger than its own."""
    from lassynth import LatticeSurgerySynthesizer
    depth = specification["max_k"] - 1
    path = CACHE / f"{_key(specification, given_vals)}.json"
    if cache and path.exists():
        hit = json.loads(path.read_text())
        if hit["status"] != TIMEOUT or hit["timeout"] >= timeout:
            return Result(hit["status"], depth, hit["seconds"], hit["lasre"])
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        wrapper = Path(tmp) / "kissat"
        wrapper.write_text(f'#!/bin/bash\nexec "{kissat_binary()}" --time={max(1, int(timeout))} "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        synthesizer = LatticeSurgerySynthesizer(solver="kissat", kissat_dir=tmp)
        try:
            solution = synthesizer.solve(specification=specification, given_vals=given_vals)
            result = Result(UNSAT, depth, 0.0) if solution is None else Result(SAT, depth, 0.0, solution.lasre)
        except ValueError as error:
            if "return code" not in str(error):
                raise
            result = Result(TIMEOUT, depth, 0.0)
    result = Result(result.status, depth, time.perf_counter() - t0, result.lasre)
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps({"status": result.status, "seconds": result.seconds, "timeout": timeout,
                                        "lasre": result.lasre}, default=list))
        tmp_path.replace(path)
    return result


def check(window: Window, program: Program, problem: Problem, timeout: int = 60, **options) -> Result:
    """The window's spec at its own depth with the tile program's pipes given."""
    return solve(spec(window, program, problem, **options), geometry(window, program), timeout)


def minimize(window: Window, program: Program, problem: Problem, timeout: int = 60, floor: int = 1,
             **options) -> List[Result]:
    """Results from `floor` layers upwards to the first satisfiable depth, the window's
    own depth the last one tried. A depth that ran out of time counts as not found and
    the search goes on above it."""
    out = []
    for depth in range(floor, layers(window) + 1):
        try:
            s = spec(window, program, problem, depth=depth, **options)
        except ValueError:      # too shallow for the magic states of one tile
            continue
        out.append(solve(s, timeout=timeout))
        if out[-1].status == SAT:
            break
    return out


def best(results: List[Result]) -> Optional[Result]:
    """The shallowest satisfiable result."""
    sat = [r for r in results if r.status == SAT]
    return min(sat, key=lambda r: r.depth) if sat else None


def cell(window: Window, program: Program, problem: Problem, timeout: int = 60, **options) -> Cell:
    """The shallowest cell found. When no depth below the window's own answers sat in
    time, the tile program's own pipes make the cell: the window unchanged, in
    lassynth's form."""
    results = minimize(window, program, problem, timeout, **options)
    found = best(results)
    below = [r for r in results if found is None or r.depth < found.depth]
    proven = found is not None and all(r.status == UNSAT for r in below)
    if found is not None and found.depth < layers(window):
        return Cell.of(window, found.lasre, proven)
    # nothing shallower: keep the tile program's own routing, which is the plainer solution
    own = check(window, program, problem, max(timeout, 120), **options)
    if own.status != SAT:
        raise RuntimeError(f"window {window.box} steps {window.start}..{window.stop}: the tile geometry did not check ({own.status})")
    return Cell.of(window, own.lasre, proven)


def cells(slabs: Sequence[Slab], program: Program, problem: Problem, timeout: int = 60,
          report=None, **options) -> List[Tuple[Slab, List[Cell]]]:
    """One cell per window of every slab, in slab order. `report(slab, cell)` is called
    after each cell."""
    out = []
    for slab in slabs:
        made = []
        for w in slab.windows:
            made.append(cell(w, program, problem, timeout, **options))
            if report is not None:
                report(slab, made[-1])
        out.append((slab, made))
    return out
