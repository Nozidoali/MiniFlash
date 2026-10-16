"""Compile a call tree bottom-up on one virtual mapping.

Leaves match exact pair sequences on the same global qubits; parents match their
ordered children. All program qubits
remain obstacles when routing a cell. The composer preserves each cell's step
order and grouping, releases qubits after their last use, and enforces the flat
pair DAG and magic supply globally. Compaction happens only after composition.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..circuit import Problem
from ..hierarchy import Hierarchy, Node
from ..mapping import Coord, Mapping
from ..route import Route, RouteError, Schedule, segments_of
from .placement import Workload
from .routing import candidates


@dataclass(frozen=True)
class Call:
    cell: int
    start: int
    stop: int


@dataclass(frozen=True)
class Cell:
    problem: Problem
    calls: Tuple[Call, ...] = ()


@dataclass(frozen=True)
class Plan:
    """Interned cells in postorder; call ranges are local to their parent.

    Frequencies count expanded invocations at every level, including the root
    and direct gate runs. Only leaves contribute gates to the placement proxy.
    """
    cells: Tuple[Cell, ...]
    root: Optional[int]
    frequencies: Tuple[int, ...]
    bindings: Dict[Tuple[str, Tuple[int, ...]], int]

    def workload(self, n_qubits: int) -> Workload:
        """Leaf-local ASAP layers weighted by expanded invocation count."""
        pairs, layers, frequencies = [], [], []
        for cell, count in zip(self.cells, self.frequencies):
            if cell.calls:
                continue
            offset = len(pairs)
            layers.extend([[offset + p for p in layer] for layer in cell.problem.layers()])
            frequencies.extend([count] * cell.problem.depth)
            pairs.extend(cell.problem.pairs)
        return Workload(Problem.from_pairs(n_qubits, pairs), layers, frequencies)

    def stats(self) -> dict:
        leaves = [i for i, cell in enumerate(self.cells) if not cell.calls]
        return dict(cells=len(self.cells), calls=sum(self.frequencies), leaf_cells=len(leaves),
                    composed_cells=len(self.cells) - len(leaves),
                    leaf_calls=sum(self.frequencies[i] for i in leaves),
                    routed_pairs=sum(len(self.cells[i].problem.pairs) for i in leaves),
                    total_pairs=0 if self.root is None else len(self.cells[self.root].problem.pairs))


def extract(hierarchy: Hierarchy) -> Plan:
    """Intern every nonempty call, without a size cutoff.

    Single-qubit Cliffords are free in the input pair model and do not enter the
    leaf key. T and T-dagger remain distinct. Parent keys preserve child boundaries
    as well as global qubits. Repeated bound definitions skip subtree extraction.
    """
    problem = hierarchy.problem
    cells, ids, bindings = [], {}, {}

    def intern(start, stop, calls=()):
        pairs = problem.pairs[start:stop]
        key = ("parent", tuple(call.cell for call in calls)) if calls else ("leaf", pairs)
        if key not in ids:
            ids[key] = len(cells)
            cells.append(Cell(Problem.from_pairs(problem.n_qubits, pairs), tuple(calls)))
        return ids[key]

    def visit(node: Node):
        binding = (node.name, node.qubits)
        if binding in bindings:
            return bindings[binding]
        children = [child for child in node.children if child.stop > child.start]
        calls = []
        at = node.start
        for child in children:
            if at < child.start:
                calls.append(Call(intern(at, child.start), at - node.start, child.start - node.start))
            calls.append(Call(visit(child), child.start - node.start, child.stop - node.start))
            at = child.stop
        if children and at < node.stop:
            calls.append(Call(intern(at, node.stop), at - node.start, node.stop - node.start))
        cell = intern(node.start, node.stop, calls)
        bindings[binding] = cell
        return cell

    root = visit(hierarchy.tree) if problem.pairs else None
    frequencies = [0] * len(cells)
    if root is not None:
        frequencies[root] = 1
    for i in reversed(range(len(cells))):
        for call in cells[i].calls:
            frequencies[call.cell] += frequencies[i]
    return Plan(tuple(cells), root, tuple(frequencies), bindings)


def _shortest(problem: Problem, mapping: Mapping, i: int, occupied: Set[Coord],
              consumed: Set[Coord]) -> Optional[Route]:
    """Breadth-first fallback with unrestricted bends, after the candidate search."""
    pair = problem.pairs[i]
    targets = {}
    if pair.is_magic:
        for magic in sorted(set(mapping.magic) - consumed):
            for tile in mapping.magic_side(magic):
                targets.setdefault(tile, magic)
    else:
        targets = dict.fromkeys(mapping.x_side(pair.x))
    previous = {t: None for t in mapping.z_side(pair.z) if mapping.is_free(t) and t not in occupied}
    queue = deque(previous)
    while queue:
        tile = queue.popleft()
        if tile in targets:
            path = [tile]
            while previous[path[-1]] is not None:
                path.append(previous[path[-1]])
            return Route(i, segments_of(path[::-1]), targets[tile])
        for nxt in mapping.vertical(tile) + mapping.horizontal(tile):
            if nxt not in previous and nxt not in occupied and mapping.is_free(nxt):
                previous[nxt] = tile
                queue.append(nxt)
    return None


def _fit(step: List[Route], problem: Problem, mapping: Mapping, occupied: Set[Coord],
         consumed: Set[Coord]) -> Optional[List[Route]]:
    """Try a whole cell step transactionally; a failed attempt reserves nothing."""
    occupied, consumed = set(occupied), set(consumed)
    placed = []
    for cached in step:
        route = cached
        if not occupied.isdisjoint(route.tiles()) or route.magic in consumed:
            choices = candidates(problem, mapping, route.pair, occupied, consumed)
            route = min(choices, key=lambda r: r.length) if choices else _shortest(problem, mapping, route.pair, occupied, consumed)
            if route is None:
                return None
        placed.append(route)
        occupied.update(route.tiles())
        if route.magic is not None:
            consumed.add(route.magic)
    return placed


def compose(problem: Problem, mapping: Mapping, calls: Sequence[Call], templates: List[Schedule],
            magic_cooldown: int = 1, factories: int = 0, period: int = 11) -> Schedule:
    """Schedule ready cell steps in call order, with global dependencies/resources.

    The step DAG adds template-order edges to the flat pair DAG. Chronological
    scheduling makes the supply ledger global even when calls overlap. A ready
    step waits as a unit if its routes or magic states cannot all fit.
    """
    groups, predecessors = [], []
    owner = [-1] * len(problem.pairs)
    for call in calls:
        previous = None
        for step in templates[call.cell]:
            if not step:
                continue  # templates are routed with unlimited supply
            group = len(groups)
            groups.append([Route(call.start + r.pair, r.segments, r.magic) for r in step])
            predecessors.append(set() if previous is None else {previous})
            for route in groups[-1]:
                owner[route.pair] = group
            previous = group
    if any(group < 0 for group in owner):
        raise RouteError("cell templates do not cover every pair")
    for i, preds in enumerate(problem.preds):
        predecessors[owner[i]].update(owner[j] for j in preds)
    successors = [[] for _ in groups]
    for i, preds in enumerate(predecessors):
        for j in preds:
            successors[j].append(i)
    remaining = [len(preds) for preds in predecessors]
    ready = [i for i, count in enumerate(remaining) if not count]
    schedule: Schedule = []
    busy_until, states, done = {}, 0, 0
    while ready:
        t = len(schedule)
        consumed = {m for m, until in busy_until.items() if until > t}
        occupied, placed, waiting, finished = set(), [], [], []
        stock = (t * factories) // period - states if factories else None
        for i in ready:
            demand = sum(r.magic is not None for r in groups[i])
            routes = None if stock is not None and demand > stock else _fit(groups[i], problem, mapping, occupied, consumed)
            if routes is None:
                waiting.append(i)
                continue
            placed.extend(routes)
            finished.append(i)
            for route in routes:
                occupied.update(route.tiles())
                if route.magic is not None:
                    consumed.add(route.magic)
                    busy_until[route.magic] = t + magic_cooldown
                    states += 1
            if stock is not None:
                stock -= demand
        if not placed and not factories and not any(until > t for until in busy_until.values()):
            raise RouteError("no ready cell step can be routed on the common mapping")
        schedule.append(placed)
        done += len(finished)
        for i in finished:
            for j in successors[i]:
                remaining[j] -= 1
                if not remaining[j]:
                    waiting.append(j)
        ready = sorted(waiting)
    if done != len(groups):
        raise RouteError("cell step order conflicts with pair dependencies")
    return schedule
