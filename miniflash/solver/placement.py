"""Placement: where each qubit sits on the array.

`place(problem, shape)` puts qubits in index order on DasCot's sparse square (the
reference point). `place(problem, shape, spectral=True)` is "+ Placement":
a placed order on the paired-lane array instead of index order.

The virtual map is seeded spectrally from the interaction graph: the Fiedler vector
of its Laplacian groups qubits into site rows; the next mode orders each row,
so qubits that interact often land near each other. A short simulated anneal then
swaps qubits between sites (empty sites included) to lower a routing proxy summed
over the ASAP layers of the DAG:

    COLL * (overlaps detected by the layer scan)
  + LANE * (rows and columns beyond the site rows and columns those routes load)
  + LEN  * (total tentative route length)

A tentative route runs from the lane-row tile on the Z party's Z side along that
lane row, then along the lane column beside the X party to its X side (an L), or
along the lane row to the nearest row-end magic tile, ignoring other routes. Every
qubit faces the same way, so the Z side is always a lane row and the X side a lane
column. Only the layers touching the
two swapped qubits are re-scored, so a proposal costs work proportional to their
degree. The array is the paired-lane one (Mapping.paired): every two site rows share
a lane and every two site columns share a lane, magic at the row ends. The later
stages read the placed order through the returned `Layout`.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

from ..circuit import Problem
from ..mapping import Coord, Layout, Mapping

COLL, LANE, LEN = 6.0, 6.0, 0.25


def spectral_order(problem: Problem, site_rows: int, site_cols: int) -> Dict[int, Tuple[int, int]]:
    """Qubit -> virtual site on a site_rows x site_cols array from the Laplacian's two
    smallest nontrivial modes: the Fiedler vector ranks rows, the next mode columns."""
    n = problem.n_qubits
    adjacency = np.full((n, n), 1e-3)
    np.fill_diagonal(adjacency, 0.0)
    for (a, b), w in problem.interaction().items():
        adjacency[a, b] += w
        adjacency[b, a] += w
    laplacian = np.diag(adjacency.sum(1)) - adjacency
    _vals, vecs = np.linalg.eigh(laplacian)
    row_scores = vecs[:, 1] if n > 1 else np.zeros(n)
    col_scores = vecs[:, 2] if n > 2 else np.zeros(n)
    by_row = sorted(range(n), key=lambda q: (row_scores[q], q))
    order = {}
    for i in range(site_rows):
        chunk = sorted(by_row[i * site_cols:(i + 1) * site_cols], key=lambda q: (col_scores[q], q))
        for j, q in enumerate(chunk):
            order[q] = (i, j)
    return order


class _Proxy:
    """Layered routing proxy with delta re-scoring per move."""

    def __init__(self, problem: Problem, mapping: Mapping, weights=(COLL, LANE, LEN)):
        self.problem = problem
        self.mapping = mapping
        self.w_coll, self.w_lane, self.w_len = weights
        self.tiles = dict(mapping.qubits)
        self.site_rows = {t[0] for t in mapping.qubits.values()}
        self.site_cols = {t[1] for t in mapping.qubits.values()}
        self.magic = sorted(mapping.magic)
        self.side_magic = [t for t in self.magic if t[1] in (0, mapping.width - 1)]
        self.lane_rows = {r for r in range(mapping.height) if r not in self.site_rows}
        self.lane_cols = {c for c in range(mapping.width) if c not in self.site_cols}
        self.layers = problem.layers()
        self.of_qubit: Dict[int, List[int]] = {q: [] for q in problem.qubits}
        for li, layer in enumerate(self.layers):
            seen = set()
            for i in layer:
                for q in problem.pairs[i].qubits:
                    if q not in seen:
                        self.of_qubit[q].append(li)
                        seen.add(q)
        # A pair's tentative route depends only on its parties' tiles, so it
        # is cached on that state: a proposal re-derives the routes of the moved qubits' pairs
        # and reuses every other route in the touched layers.
        self._routes: Dict[int, Dict[tuple, Tuple[List[Coord], Set[int], Set[int]]]] = {}
        self._magic_near: Dict[Coord, Coord] = {}
        self.layer_cost = [self.score(li) for li in range(len(self.layers))]
        self.total = sum(self.layer_cost)

    def side_tile(self, q: int, z: bool) -> Coord:
        """The lane-row tile on qubit q's Z side (z=True) or the lane-column tile on its X side."""
        r, c = self.tiles[q]
        if z:
            return (r - 1, c) if r - 1 in self.lane_rows else (r + 1, c)
        return (r, c - 1) if c - 1 in self.lane_cols else (r, c + 1)

    @staticmethod
    def _chain(points: List[Coord]) -> List[Coord]:
        out = [points[0]]
        for a, b in zip(points, points[1:]):
            if a[0] == b[0]:
                step = 1 if b[1] >= a[1] else -1
                out.extend((a[0], c) for c in range(a[1] + step, b[1] + step, step))
            else:
                step = 1 if b[0] >= a[0] else -1
                out.extend((r, a[1]) for r in range(a[0] + step, b[0] + step, step))
        return out

    def route_tiles(self, i: int) -> Tuple[List[Coord], Set[int], Set[int]]:
        """Tentative route, occupancy ignored (see module docstring); cached on the parties' state."""
        pair = self.problem.pairs[i]
        if pair.is_magic:
            key = (self.tiles[pair.z],)
        else:
            key = (self.tiles[pair.z], self.tiles[pair.x])
        recent = self._routes.get(i)
        if recent is None:
            recent = self._routes[i] = {}
        result = recent.get(key)
        if result is None:
            if len(recent) >= 4:          # a rejected proposal restores an earlier state: keep a few
                recent.pop(next(iter(recent)))
            result = recent[key] = self._route_tiles(i)
        return result

    def _route_tiles(self, i: int) -> Tuple[List[Coord], Set[int], Set[int]]:
        pair = self.problem.pairs[i]
        start = self.side_tile(pair.z, z=True)           # on a lane row
        magic = None
        if pair.is_magic:
            rq, cq = self.tiles[pair.z]
            magic = self._magic_near.get((rq, cq))
            if magic is None:
                pool = self.side_magic or self.magic
                magic = self._magic_near[(rq, cq)] = min(pool, key=lambda t: (abs(t[0] - rq) + abs(t[1] - cq), t))
            points = [start, (start[0], magic[1])]      # along the lane row to the tile beside the magic tile
        else:
            end = self.side_tile(pair.x, z=False)        # on a lane column
            points = [start, (start[0], end[1]), end]    # row then column: the L
        tiles = self._chain(points)
        rows = {t[0] for t in points}
        cols = {t[1] for t in points}
        if magic is not None:
            rows.add(magic[0])
            cols.add(magic[1])
        return tiles, rows, cols

    def score(self, li: int) -> float:
        # Each tile remembers its most recent route. A route counts each previous
        # owner it encounters once; this is a proxy, not an all-pairs conflict count.
        seen: Dict[Coord, int] = {}
        coll = 0
        rows: Set[int] = set()
        cols: Set[int] = set()
        length = 0
        for i in self.layers[li]:
            tiles, r, c = self.route_tiles(i)
            length += len(tiles)
            rows |= r
            cols |= c
            hit = set()
            for t in tiles:
                if t in seen:
                    hit.add(seen[t])
                seen[t] = i
            coll += len(hit)
        lanes = len(rows - self.site_rows) + len(cols - self.site_cols)
        return self.w_coll * coll + self.w_lane * lanes + self.w_len * length

    def swap_delta(self, a: int, b: Optional[int], site_b: Coord) -> Tuple[float, List[int], Dict[int, float]]:
        """Cost change if qubit a moves to site_b and qubit b (if any) to a's site."""
        touched = sorted(set(self.of_qubit[a]) | (set(self.of_qubit[b]) if b is not None else set()))
        site_a = self.tiles[a]
        self.tiles[a] = site_b
        if b is not None:
            self.tiles[b] = site_a
        new = {li: self.score(li) for li in touched}
        self.tiles[a] = site_a
        if b is not None:
            self.tiles[b] = site_b
        return sum(new[li] - self.layer_cost[li] for li in touched), touched, new

    def apply(self, a: int, b: Optional[int], site_b: Coord, new: Dict[int, float]):
        site_a = self.tiles[a]
        self.tiles[a] = site_b
        if b is not None:
            self.tiles[b] = site_a
        for li, v in new.items():
            self.total += v - self.layer_cost[li]
            self.layer_cost[li] = v


@dataclass(frozen=True)
class Placement:
    mapping: Mapping
    layout: Layout
    initial_cost: float
    final_cost: float


def square_shape(n_qubits: int, shape: Optional[Tuple[int, int]] = None) -> Tuple[int, int]:
    """(site_rows, site_cols): the given shape, or the smallest square."""
    return shape if shape is not None else (math.ceil(math.sqrt(n_qubits)),) * 2


def place(problem: Problem, shape: Optional[Tuple[int, int]] = None, spectral: bool = False, seed: int = 0,
          iters: Optional[int] = None, anneal: bool = True,
          weights: Tuple[float, float, float] = (COLL, LANE, LEN)) -> Placement:
    """Index order on the sparse square, or (`spectral`) the spectral seed and anneal on
    the paired-lane array, of the given shape (the smallest square by default);
    `iters` defaults to 100 proposals per qubit."""
    n = problem.n_qubits
    site_rows, site_cols = square_shape(n, shape)
    if not spectral:
        return Placement(Mapping.sparse(site_rows, site_cols, n), Layout.identity(n, (site_rows, site_cols)), 0.0, 0.0)
    order = spectral_order(problem, site_rows, site_cols)
    base = Mapping.paired(site_rows, site_cols, n)
    full = Mapping.paired(site_rows, site_cols, site_rows * site_cols)      # every site filled, to read the geometry
    row_pos = sorted({t[0] for t in full.qubits.values()})
    col_pos = sorted({t[1] for t in full.qubits.values()})
    site_of = {(i, j): (row_pos[i], col_pos[j]) for i in range(site_rows) for j in range(site_cols)}
    mapping = Mapping(base.height, base.width, {q: site_of[s] for q, s in order.items()}, base.magic)
    proxy = _Proxy(problem, mapping, weights)
    initial_cost = proxy.total
    if anneal and n > 1:
        rng = random.Random(seed)
        iters = iters if iters is not None else 100 * n
        sites = list(site_of.values())
        occupant = {t: q for q, t in proxy.tiles.items()}
        t0, t1 = max(1.0, 0.05 * proxy.total / max(1, len(proxy.layers))), 0.05
        for it in range(iters):
            temp = t0 * (t1 / t0) ** (it / max(1, iters - 1))
            a = rng.randrange(n)
            site_b = sites[rng.randrange(len(sites))]
            if site_b == proxy.tiles[a]:
                continue
            b = occupant.get(site_b)
            delta, touched, new = proxy.swap_delta(a, b, site_b)
            if delta <= 0 or rng.random() < math.exp(-delta / temp):
                site_a = proxy.tiles[a]
                proxy.apply(a, b, site_b, new)
                occupant[site_b] = a
                if b is not None:
                    occupant[site_a] = b
                else:
                    del occupant[site_a]
    inv = {t: s for s, t in site_of.items()}
    layout = Layout(site_rows, site_cols, {q: inv[t] for q, t in proxy.tiles.items()})
    return Placement(Mapping(base.height, base.width, dict(proxy.tiles), base.magic),
                     layout, initial_cost, proxy.total)
