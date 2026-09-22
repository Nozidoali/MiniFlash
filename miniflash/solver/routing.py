"""Routing: the criticality-driven router with lane budgets ("+ Routing";
with partition budgets, "+ Spacing").

`route(problem, mapping, rb, cb)` routes ready pairs while limiting the lines retained by each partition.

Pairs are ordered by longest remaining dependency chain, then input index. A
small frontier uses branch and bound over generated route candidates, maximizing
criticality minus the cost of new lines; larger frontiers use greedy selection
by route length plus line cost. Both reserve route tiles and consumed magic.

Strict placement includes site lines and accumulated partition usage in each
axis budget. If no step is selected, close a nonempty partition and retry. A
fresh partition can take an unconstrained greedy step to make progress, so the
budget is not an unconditional bound on the resulting chip size.

The magic supply comes from ``factory.supply_limits``. ``magic_cooldown`` models
one factory per delivery tile: a tile consumed at routed step t is unavailable
until step t + magic_cooldown, so 1 is unlimited supply and 11 admits one state
per tile every 11 steps. ``factories`` models the supply globally: F
factories produce from routed step 0, one state per ``period`` steps each, so the
k-th state consumed anywhere on the chip may not be consumed before step
k x period / F (floor(t x F / period) states exist by step t); 0 leaves the supply
unlimited. The router counts routed steps only, so walk steps inserted later only
widen the gap. When every ready pair is a T that waits for a tile or a state, the
router emits an idle step.

spacing.stage compacts the resulting windows and inserts walks. See
``docs/05-budget.md`` for the objectives and ``docs/09-exact-routing.md`` for the
scope of exactness. Candidates include straight, L, and limited three-segment
paths from the shared search; this solver does not use a BFS fallback.
"""
from __future__ import annotations

from typing import Dict, List, Set, Tuple

from ..circuit import Problem
from ..mapping import Coord, Mapping
from ..route import Route, RouteError, Schedule, free_routes

NEAREST = 4   # magic tiles considered per T pair, by distance


def lines_of(route: Route) -> Tuple[Set[int], Set[int]]:
    """Rows and columns a route makes load-bearing (its segment endpoints and magic tile)."""
    rows, cols = set(), set()
    for s in route.segments:
        rows.update((s.start[0], s.end[0]))
        cols.update((s.start[1], s.end[1]))
    if route.magic is not None:
        rows.add(route.magic[0])
        cols.add(route.magic[1])
    return rows, cols


def candidates(problem: Problem, mapping: Mapping, i: int, occupied: Set[Coord], consumed: Set[Coord]) -> List[Route]:
    """Free routes from the Z side of the Z party to the X side of the X party, or to any
    side of one of the NEAREST reachable magic tiles."""
    pair = problem.pairs[i]
    starts = mapping.z_side(pair.z)
    if not pair.is_magic:
        return free_routes(i, starts, mapping.x_side(pair.x), mapping, blocked=occupied)
    z_tile = mapping.qubits[pair.z]
    out = []
    reachable = 0
    for m in sorted((m for m in mapping.magic if m not in consumed),
                    key=lambda m: (abs(m[0] - z_tile[0]) + abs(m[1] - z_tile[1]), m)):
        routes = free_routes(i, starts, mapping.magic_side(m), mapping, magic=m, blocked=occupied)
        if routes:
            out.extend(routes)
            reachable += 1
            if reachable == NEAREST:
                break
    return out

def route(problem: Problem, mapping: Mapping, rb: int, cb: int, share: float = 4.0, exact: int = 12,
          magic_cooldown: int = 1, factories: int = 0, period: int = 11) -> Tuple[Schedule, List[Tuple[int, int]]]:
    """The criticality-driven router with lane budgets (rb, cb): the schedule and the
    partition windows it closed."""
    site_rows = {t[0] for t in mapping.qubits.values()}
    site_cols = {t[1] for t in mapping.qubits.values()}
    succs = problem.succs()
    crit = problem.criticality()
    remaining = [len(p) for p in problem.preds]
    frontier = order(crit, [i for i, n in enumerate(remaining) if n == 0])
    schedule: Schedule = []
    windows: List[Tuple[int, int]] = []
    start = 0
    part_rows: Set[int] = set()
    part_cols: Set[int] = set()
    busy_until: Dict[Coord, int] = {}   # magic tile -> first step it may be consumed again
    states = 0                          # magic states consumed so far
    while frontier:
        t = len(schedule)
        fresh = t == start
        busy = {m for m, until in busy_until.items() if until > t}
        stock = (t * factories) // period - states if factories else None   # states produced but not yet consumed
        placed, waiting, step_rows, step_cols = select(frontier, problem, mapping, site_rows, site_cols, part_rows, part_cols, rb, cb, True, busy, share, exact, crit, stock)
        if not placed and (busy or stock == 0) and all(problem.pairs[i].is_magic for i in frontier):
            schedule.append([])   # every ready pair waits for a magic tile or a magic state: idle step, same partition
            continue
        if not placed and not fresh:
            # nothing fits beside the open partition: close it and retry on a fresh one
            windows.append((start, t))
            start, part_rows, part_cols = t, set(), set()
            continue
        if not placed:
            placed, waiting, step_rows, step_cols = select(frontier, problem, mapping, site_rows, site_cols, part_rows, part_cols, rb, cb, False, busy, share, exact, crit, stock)
        if not placed:
            raise RouteError(f"step {t}: no pair in the frontier {frontier} can be routed")
        part_rows |= step_rows
        part_cols |= step_cols
        for rt in placed:
            if rt.magic is not None:
                busy_until[rt.magic] = t + magic_cooldown
                states += 1
        schedule.append(placed)
        for rt in placed:
            for j in succs[rt.pair]:
                remaining[j] -= 1
                if remaining[j] == 0:
                    waiting.append(j)
        frontier = order(crit, waiting)
    windows.append((start, len(schedule)))
    if sum(len(s) for s in schedule) != len(problem.pairs):
        raise RouteError("not every pair was scheduled")
    return schedule, windows

def order(crit, pairs) -> List[int]:
    """Criticality first, program order on ties."""
    return sorted(pairs, key=lambda i: (-crit[i], i))

def select(frontier, problem, mapping, site_rows, site_cols, part_rows, part_cols, rb, cb, strict, busy, share, exact, crit, stock=None):
    """One step: exact selection on a small front, greedy otherwise. `busy` holds the
    magic tiles still recovering from an earlier consumption; `stock` caps the states
    this step may consume (None: unlimited).
    Returns (placed, waiting, rows, cols loaded this step)."""
    if strict and len(frontier) <= exact:
        return exact_select(frontier, problem, mapping, site_rows, site_cols, part_rows, part_cols, rb, cb, busy, share, crit, stock)
    return greedy_select(frontier, problem, mapping, site_rows, site_cols, part_rows, part_cols, rb, cb, strict, busy, share, crit, stock)

def exact_select(frontier, problem, mapping, site_rows, site_cols, part_rows, part_cols, rb, cb, busy, share, crit, stock=None):
    """Branch and bound over the front in order: for each pair try every free candidate,
    then skipping it. Score = criticality placed - share * new lanes; the bound adds the
    criticality of every undecided pair. Candidates are tried shortest first and ties
    keep the first assignment found."""
    criticality = crit
    # Each candidate's tiles and lines are constants of the step: derive them once.
    options = {
        i: [(cand, frozenset(cand.tiles()), *lines_of(cand))
            for cand in sorted(candidates(problem, mapping, i, set(), set(busy)), key=lambda route: route.length)]
        for i in frontier
    }
    total_criticality = sum(criticality[i] for i in frontier)
    best_score: Optional[float] = None
    best_chosen: Dict[int, Route] = {}
    occupied: Set[Coord] = set()
    consumed: Set[Coord] = set(busy)
    loaded_rows: Set[int] = set(site_rows | part_rows)
    loaded_cols: Set[int] = set(site_cols | part_cols)
    chosen: Dict[int, Route] = {}
    taken = [0]                     # magic states chosen on the current branch

    def search(index: int, score: float) -> None:
        nonlocal best_score, best_chosen
        # Optimistic bound: place every remaining pair without opening any lanes.
        remaining_reward = sum(criticality[i] for i in frontier[index:])
        if best_score is not None and score + remaining_reward <= best_score:
            return
        if index == len(frontier):
            if best_score is None or score > best_score:
                best_score, best_chosen = score, dict(chosen)
            return
        i = frontier[index]
        for cand, tiles, rows, cols in options[i]:
            if cand.magic is not None and (cand.magic in consumed or (stock is not None and taken[0] >= stock)):
                continue
            if not occupied.isdisjoint(tiles):
                continue
            new_rows, new_cols = rows - loaded_rows, cols - loaded_cols
            if len(loaded_rows | new_rows) > rb or len(loaded_cols | new_cols) > cb:
                continue
            occupied.update(tiles)
            if cand.magic is not None:
                consumed.add(cand.magic)
                taken[0] += 1
            loaded_rows.update(new_rows)
            loaded_cols.update(new_cols)
            chosen[i] = cand
            lane_cost = share * (len(new_rows) + len(new_cols))
            search(index + 1, score + criticality[i] - lane_cost)
            # Undo only this candidate's additions before trying the next branch.
            del chosen[i]
            loaded_rows.difference_update(new_rows)
            loaded_cols.difference_update(new_cols)
            if cand.magic is not None:
                consumed.discard(cand.magic)
                taken[0] -= 1
            occupied.difference_update(tiles)
            if best_score == total_criticality:
                return
        search(index + 1, score)  # Defer this pair to a later step.

    search(0, 0.0)
    placed = [best_chosen[i] for i in frontier if i in best_chosen]
    waiting = [i for i in frontier if i not in best_chosen]
    step_rows: Set[int] = set()
    step_cols: Set[int] = set()
    for r in placed:
        rows, cols = lines_of(r)
        step_rows |= rows
        step_cols |= cols
    return placed, waiting, step_rows, step_cols

def greedy_select(frontier, problem, mapping, site_rows, site_cols, part_rows, part_cols, rb, cb, strict, busy, share, crit, stock=None):
    occupied: Set[Coord] = set()
    consumed: Set[Coord] = set(busy)
    step_rows: Set[int] = set()
    step_cols: Set[int] = set()
    placed: List[Route] = []
    waiting: List[int] = []
    taken = 0
    for i in frontier:
        best, best_score = None, None
        if problem.pairs[i].is_magic and stock is not None and taken >= stock:
            waiting.append(i)
            continue
        for cand in candidates(problem, mapping, i, occupied, consumed):
            rows, cols = lines_of(cand)
            new_rows = rows - site_rows - step_rows - part_rows
            new_cols = cols - site_cols - step_cols - part_cols
            if strict and (len(site_rows | part_rows | step_rows | new_rows) > rb
                           or len(site_cols | part_cols | step_cols | new_cols) > cb):
                continue
            score = cand.length + share * (len(new_rows) + len(new_cols))
            if best is None or score < best_score:
                best, best_score = cand, score
        if best is None:
            waiting.append(i)
            continue
        placed.append(best)
        occupied.update(best.tiles())
        if best.magic is not None:
            consumed.add(best.magic)
            taken += 1
        rows, cols = lines_of(best)
        step_rows |= rows
        step_cols |= cols
    return placed, waiting, step_rows, step_cols
