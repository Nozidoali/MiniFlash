"""Windows, slabs and cells in 3-D.

    window_box   the cube-coordinate corners of a window, on the glTF lattice
    boxes        one translucent (or opaque) box per window, coloured by slab, for
                 :func:`miniflash.gltf.write_gltf`'s ``overlays``
    timeline     where every step and slab starts once the cells are in, in layers
    lower_cells  the pipes of a program whose slabs are replaced by cells
    write_cells  that scene as a glTF file

The overlay view keeps every pipe and lays the boxes over them; the carve view
(``write_gltf(..., carve=True)``) removes what is inside the boxes so each window
stands as an opaque cell: the program after coarsening, before any cell is
resynthesised.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..gltf import STRIDE, _Pipe, _merge_pipes, _qubit_pipes, write_scene
from ..program import MAGIC, Program
from .cell import Cell
from .window import LAYERS_PER_STEP, Slab, Window

# colours the pipe materials do not use (they take red, blue, grey, yellow, green, orange)
PALETTE = ((0.55, 0.25, 0.80), (0.10, 0.65, 0.70), (0.85, 0.30, 0.60),
           (0.45, 0.60, 0.10), (0.60, 0.40, 0.20), (0.20, 0.45, 0.85))


def window_box(window: Window) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Inclusive (lo, hi) cube corners: columns along I, rows along J, both layers of
    every step along K."""
    lo = (STRIDE * window.box.c0, STRIDE * window.box.r0, STRIDE * LAYERS_PER_STEP * window.start)
    hi = (STRIDE * window.box.c1, STRIDE * window.box.r1, STRIDE * (LAYERS_PER_STEP * window.stop - 1))
    return lo, hi


def boxes(slabs: Iterable[Slab], alpha: float = 0.35) -> List[Tuple[tuple, tuple, tuple]]:
    """(lo, hi, rgba) per window, the windows of one slab sharing a colour and consecutive
    slabs cycling through the palette. ``alpha`` 1.0 makes opaque cells."""
    out = []
    for i, slab in enumerate(slabs):
        rgb = PALETTE[i % len(PALETTE)]
        for w in slab.windows:
            lo, hi = window_box(w)
            out.append((lo, hi, rgb + (alpha,)))
    return out


Y_CUBE = (0.90, 0.25, 0.65, 1.0)
Plan = Sequence[Tuple[Slab, Sequence[Cell]]]


def slab_depth(slab: Slab, cells: Sequence[Cell], compact: bool = True) -> int:
    """Layers the slab takes: its deepest cell, or its own two per step when not compact."""
    if not compact:
        return LAYERS_PER_STEP * slab.steps
    return max((c.depth for c in cells), default=LAYERS_PER_STEP * slab.steps)


def timeline(program: Program, plan: Plan, compact: bool = True) -> Tuple[Dict[int, int], int]:
    """({step: first layer}, total layers). A step inside a slab maps to the slab's first
    layer; a step outside every slab keeps its two layers."""
    by_start = {slab.start: (slab, cells) for slab, cells in plan}
    base, layer, t = {}, 0, 0
    while t < len(program.steps):
        if t in by_start:
            slab, cells = by_start[t]
            for u in range(slab.start, slab.stop):
                base[u] = layer
            layer += slab_depth(slab, cells, compact)
            t = slab.stop
        else:
            base[t] = layer
            layer += LAYERS_PER_STEP
            t += 1
    return base, layer


def lower_cells(program: Program, plan: Plan, compact: bool = True, outline: float = 0.12):
    """Pipes, cubes, magic cubes and overlays of `program` with the slabs of `plan`
    replaced by their cells.

    With `compact` every slab is as deep as its deepest cell and everything above moves
    down; without it the slabs keep their place and a shallow cell leaves its qubits
    idle above it. Y cubes are drawn pink; `outline` is the alpha of a translucent box
    around every cell, 0 for none.

    :returns: (pipes, cubes, magic, overlays, layers)
    """
    base, total = timeline(program, plan, compact)
    found: Dict[Tuple[tuple, tuple], _Pipe] = {}
    magic, overlays = set(), []

    def cube(tile, layer):
        return (STRIDE * tile[1], STRIDE * tile[0], STRIDE * layer)

    def put(a, b, parity, hadamard=False, walk=False):
        lo, hi = min(a, b), max(a, b)
        if lo[2] < 0 or hi[2] > STRIDE * (total - 1):
            return
        axis = "IJK"[next(x for x in range(3) if lo[x] != hi[x])]
        found[(lo, hi)] = _Pipe(lo, hi, axis, parity, hadamard=hadamard, walk=walk)

    covered = {t for slab, _ in plan for t in range(slab.start, slab.stop)}
    steps = program.steps
    for t, step in enumerate(steps):
        if t in covered:
            continue
        after = steps[t + 1] if t + 1 < len(steps) else None
        move = {2 * t: base[t], 2 * t + 1: base[t] + 1, 2 * t + 2: base.get(t + 1, total)}
        for pipe in _merge_pipes(step, t) + _qubit_pipes(step, t, after):
            a = pipe.lo[:2] + (STRIDE * move[pipe.lo[2] // STRIDE],)
            b = pipe.hi[:2] + (STRIDE * move[pipe.hi[2] // STRIDE],)
            put(a, b, pipe.parity, walk=pipe.walk)
        for v in step.of_kind(MAGIC):
            if v.id >= 0:
                magic.add(cube(v.tile, base[t]))

    for n, (slab, cells) in enumerate(plan):
        first, depth = base[slab.start], slab_depth(slab, cells, compact)
        seats = steps[slab.start].qubit_tiles()
        busy = {q for c in cells for q in c.window.participants}
        for q, tile in seats.items():
            if q not in busy:
                for layer in range(first, first + depth):
                    put(cube(tile, layer), cube(tile, layer + 1), 0)
        for c in cells:
            box = c.window.box

            def at(ijk, box=box):
                return (STRIDE * (box.c0 + ijk[0]), STRIDE * (box.r0 + ijk[1]), STRIDE * (first + ijk[2] - 1))
            for pipe in c.pipes():
                step3 = tuple(int(x == pipe.axis) for x in "IJK")
                put(at(pipe.cube), at(tuple(a + b for a, b in zip(pipe.cube, step3))), pipe.parity, hadamard=pipe.wall)
            for q in c.window.participants:
                for layer in range(first + c.depth, first + depth):
                    put(cube(seats[q], layer), cube(seats[q], layer + 1), 0)
            for m in c.magic_cubes():
                magic.add(at(m))
            for y in c.y_cubes():
                overlays.append((at(y), at(y), Y_CUBE))
            if outline > 0:
                lo = at((0, 0, 1))
                hi = at((box.width - 1, box.height - 1, c.depth))
                overlays.append((lo, hi, PALETTE[n % len(PALETTE)] + (outline,)))

    pipes = sorted(found.values(), key=lambda p: (p.lo, p.hi, p.axis))
    cubes = {p.lo for p in pipes} | {p.hi for p in pipes}
    cubes -= magic
    cubes -= {lo for lo, hi, rgba in overlays if lo == hi}
    return pipes, sorted(cubes), sorted(magic), overlays, total


def write_cells(program: Program, plan: Plan, path, compact: bool = True, outline: float = 0.12) -> int:
    """Write the program with its slabs replaced by cells; returns the layers it takes."""
    pipes, cubes, magic, overlays, total = lower_cells(program, plan, compact, outline)
    write_scene(path, pipes, cubes, magic, overlays=overlays)
    return total
