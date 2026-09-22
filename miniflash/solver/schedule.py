"""Scheduling: which pairs execute in each step, and along which routes.

`schedule(problem, mapping)` is the dependency-aware schedule
("+ Dependency"): a frontier holds the pairs whose predecessors have all finished,
seeded with the DAG's sources. Each step walks the frontier in pair order; a pair
whose shortest free candidate route exists is placed and its tiles (and magic tile,
for a T) become occupied for the rest of the step, and a pair that finds no free
route stays in the frontier. When the step ends, the successors of the placed pairs
whose other predecessors are also done join the frontier, and the occupancy clears.

With `dependency=False` (the vanilla reference point) every step executes one pair,
in program order, along the shortest legal route; nothing ever blocks.
"""
from __future__ import annotations

from typing import List, Set

from ..circuit import Problem
from ..mapping import Coord, Mapping
from ..route import Route, RouteError, Schedule, check_route, shortest_magic_route, shortest_route


def schedule(problem: Problem, mapping: Mapping, dependency: bool = True) -> Schedule:
    if not dependency:
        out = []
        for i, pair in enumerate(problem.pairs):
            r = shortest_magic_route(i, pair.z, mapping) if pair.is_magic else shortest_route(i, pair.z, pair.x, mapping)
            if r is None:
                raise RouteError(f"pair {i} {pair}: no candidate route on the mapping")
            check_route(r, pair, mapping)
            out.append([r])
        return out
    succs = problem.succs()
    remaining = [len(p) for p in problem.preds]         # unfinished predecessors per pair
    frontier = [i for i, n in enumerate(remaining) if n == 0]
    done = 0
    steps: Schedule = []
    while frontier:
        occupied: Set[Coord] = set()
        consumed: Set[Coord] = set()
        placed: List[Route] = []
        waiting: List[int] = []
        for i in frontier:
            pair = problem.pairs[i]
            if pair.is_magic:
                r = shortest_magic_route(i, pair.z, mapping, blocked=occupied, consumed=consumed)
            else:
                r = shortest_route(i, pair.z, pair.x, mapping, blocked=occupied)
            if r is None:
                waiting.append(i)
                continue
            placed.append(r)
            occupied.update(r.tiles())
            if r.magic is not None:
                consumed.add(r.magic)
        if not placed:
            raise RouteError(f"step {len(steps)}: no pair in the frontier {frontier} can be routed")
        steps.append(placed)
        done += len(placed)
        for r in placed:
            for j in succs[r.pair]:
                remaining[j] -= 1
                if remaining[j] == 0:
                    waiting.append(j)
        frontier = sorted(waiting)
    if done != len(problem.pairs):
        raise RouteError(f"{done} of {len(problem.pairs)} pairs scheduled")
    return steps
