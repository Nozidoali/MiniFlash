"""Window extraction: closed windows by component closure, slabs by coverage.

    stages          maximal runs of steps without a walk; qubits keep their tiles inside one
    close           the closed windows of one step range: components merged transitively by
                    box overlap, each class one window whose box is the union
    slabs           every step range of every stage, up to a length, whose windows all fit
                    the budget; longer ranges only grow windows, so each start stops at the
                    first range that does not fit
    extract_greedy  a greedy pick of non-overlapping slabs by estimated gain
    extract_dp      the cheapest chain of slabs through every stage, by dynamic programming
                    over the cut positions; `cost` prices a slab and `Weights` sets the terms

Closure is what makes a window closed: a component touching a window's box is
merged into it, and because qubits do not move inside a stage every later or
earlier component on a participant is inside the box too, so the box cuts nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..circuit import Problem
from ..program import IDLE, MAGIC, QUBIT, Program
from .window import Box, Budget, Component, Slab, Window


def stages(program: Program) -> List[Tuple[int, int]]:
    """(start, stop) of each maximal run of steps with edges. Walk steps and idle
    steps separate stages and belong to none."""
    out = []
    start = None
    for t, step in enumerate(program.steps):
        if step.is_walk:
            if start is not None:
                out.append((start, t))
                start = None
        elif start is None:
            start = t
    if start is not None:
        out.append((start, len(program.steps)))
    return out


def close(program: Program, start: int, stop: int) -> List[Window]:
    """The closed windows of steps start..stop-1, ordered by box position."""
    comps = [c for t in range(start, stop) for c in Component.of_step(program.steps[t], t)]
    classes: List[Tuple[Box, List[Component]]] = [(c.box, [c]) for c in comps]
    merged = True
    while merged:  # to a fixpoint: a union can overlap a class already passed over
        merged = False
        out: List[Tuple[Box, List[Component]]] = []
        for box, members in classes:
            for i, (other, others) in enumerate(out):
                if box.overlaps(other):
                    out[i] = (box.union(other), others + members)
                    merged = True
                    break
            else:
                out.append((box, members))
        classes = out
    first = program.steps[start]
    consumed_anywhere = {c.magic for c in comps if c.magic is not None}
    windows = []
    for box, members in classes:
        participants = {q for c in members for q in c.qubits}
        bystanders = {v.id: v.tile for v in first.of_kind(QUBIT) if box.contains(v.tile) and v.id not in participants}
        idle = tuple(v.tile for v in first.of_kind(MAGIC) if box.contains(v.tile) and v.tile not in consumed_anywhere)
        windows.append(Window(box, start, stop, tuple(members), bystanders, idle))
    return sorted(windows, key=lambda w: (w.box.r0, w.box.c0))


def slabs(program: Program, budget: Budget = Budget(), max_steps: int = None) -> List[Slab]:
    """Every step range inside a stage whose closed windows all fit the budget, at most
    `max_steps` long (the budget's step limit by default)."""
    if max_steps is None:
        max_steps = budget.steps if budget.steps is not None else len(program.steps)
    out = []
    for s0, s1 in stages(program):
        for start in range(s0, s1):
            for stop in range(start + 1, min(start + max_steps, s1) + 1):
                windows = close(program, start, stop)
                if not all(budget.fits(w) for w in windows):
                    break
                out.append(Slab(start, stop, tuple(windows)))
    return out


def extract_greedy(program: Program, problem: Problem, budget: Budget = Budget(), max_steps: int = None,
            min_gain: int = 1) -> List[Slab]:
    """Non-overlapping slabs picked greedily by estimated gain, longer and earlier first
    among equals, dropping those below `min_gain`. Sorted by start."""
    ranked = sorted(slabs(program, budget, max_steps),
                    key=lambda s: (-s.gain(problem), -s.steps, s.start))
    chosen: List[Slab] = []
    for s in ranked:
        if s.gain(problem) < min_gain:
            break
        if not any(s.overlaps(c) for c in chosen):
            chosen.append(s)
    return sorted(chosen, key=lambda s: s.start)


@dataclass(frozen=True)
class Weights:
    """What a chain of slabs pays, in tile-steps.

    volume  per tile-step of the estimated slab: depth x program area
    holes   per tile-step left empty above a window shallower than its slab
    cuts    per dependency edge a cut severs
    skip    per step left outside every slab, on top of that step's volume and of the
            dependency edges entering it, which it severs like any cut: a step within the
            budget is therefore always cheaper inside a slab than skipped
    """
    volume: float = 1.0
    holes: float = 1.0
    cuts: float = 2.0
    skip: float = 4.0


def cost(slab: Slab, problem: Problem, area: int, weights: Weights = Weights()) -> float:
    """Price of one slab on a program of `area` tiles."""
    return (weights.volume * slab.depth(problem) * area + weights.holes * slab.holes(problem)
            + weights.cuts * slab.cuts(problem))


def skip_cost(program: Program, problem: Problem, t: int, weights: Weights = Weights()) -> float:
    """Price of leaving step t outside every slab."""
    edges = sum(len(problem.preds[c.pair]) for c in Component.of_step(program.steps[t], t))
    return weights.volume * program.area + weights.cuts * edges + weights.skip


def extract_dp(program: Program, problem: Problem, budget: Budget = Budget(), max_steps: int = None,
              weights: Weights = Weights()) -> List[Slab]:
    """The chain of slabs of least total cost, stage by stage.

    best[t] is the least cost of cutting the stage up to step t. The last slab ends at
    t and spans 1..max_steps steps, all of its windows within the budget, or step t - 1
    is skipped: left as it is, at `skip_cost`. Every step is in a slab unless no slab
    within the budget holds it. Sorted by start.
    """
    by_range: Dict[Tuple[int, int], Slab] = {(s.start, s.stop): s for s in slabs(program, budget, max_steps)}
    longest = max((s.steps for s in by_range.values()), default=1)
    area = program.area
    chosen: List[Slab] = []
    for s0, s1 in stages(program):
        best: Dict[int, float] = {s0: 0.0}
        back: Dict[int, Optional[Slab]] = {}
        for t in range(s0 + 1, s1 + 1):
            best[t] = best[t - 1] + skip_cost(program, problem, t - 1, weights)
            back[t] = None
            for length in range(1, min(longest, t - s0) + 1):
                slab = by_range.get((t - length, t))
                if slab is None:
                    continue
                c = best[t - length] + cost(slab, problem, area, weights)
                if c < best[t]:
                    best[t], back[t] = c, slab
        t = s1
        while t > s0:
            slab = back[t]
            if slab is None:
                t -= 1
            else:
                chosen.append(slab)
                t = slab.start
    return sorted(chosen, key=lambda s: s.start)


def total(chosen: List[Slab], program: Program, problem: Problem, weights: Weights = Weights()) -> float:
    """Cost of a set of non-overlapping slabs over the whole program, the steps of every
    stage they leave out charged as skips."""
    covered = {t for s in chosen for t in range(s.start, s.stop)}
    skipped = [t for s0, s1 in stages(program) for t in range(s0, s1) if t not in covered]
    return (sum(cost(s, problem, program.area, weights) for s in chosen)
            + sum(skip_cost(program, problem, t, weights) for t in skipped))
