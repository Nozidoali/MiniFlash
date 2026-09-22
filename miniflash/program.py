"""Program: the solver's output, one sparse step graph per time step.

Each step lists the non-free tiles as vertices (coordinate, kind, id) and the
merges between them as undirected edges. Kinds are QUBIT (id = qubit index),
MAGIC (id = consuming pair, or -1 when idle), and ROUTE (id = pair). A pair that
executes at a step is the connected component holding its parties. Free tiles are
absent. Volume is steps x height x width.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Sequence, Set

from .circuit import Problem
from .mapping import Coord, Mapping
from .route import Route, check_route

QUBIT, MAGIC, ROUTE = "qubit", "magic", "route"
KINDS = (QUBIT, MAGIC, ROUTE)
IDLE = -1
Edge = FrozenSet[Coord]


@dataclass(frozen=True)
class Vertex:
    tile: Coord
    kind: str
    id: int

    def __post_init__(self):
        object.__setattr__(self, "tile", tuple(self.tile))
        if self.kind not in KINDS:
            raise ValueError(f"unknown vertex kind {self.kind}")


@dataclass
class Step:
    vertices: Dict[Coord, Vertex] = field(default_factory=dict)
    edges: Set[Edge] = field(default_factory=set)

    def add(self, tile: Coord, kind: str, id: int) -> Vertex:
        tile = tuple(tile)
        if tile in self.vertices:
            raise ValueError(f"tile {tile} already holds {self.vertices[tile]}")
        v = Vertex(tile, kind, id)
        self.vertices[tile] = v
        return v

    def link(self, a: Coord, b: Coord) -> None:
        a, b = tuple(a), tuple(b)
        if a not in self.vertices or b not in self.vertices:
            raise ValueError(f"edge {a}-{b} touches a free tile")
        self.edges.add(frozenset((a, b)))

    def of_kind(self, kind: str) -> List[Vertex]:
        return [v for v in self.vertices.values() if v.kind == kind]

    def qubit_tiles(self) -> Dict[int, Coord]:
        return {v.id: v.tile for v in self.vertices.values() if v.kind == QUBIT}

    def neighbours(self) -> Dict[Coord, Set[Coord]]:
        adj: Dict[Coord, Set[Coord]] = {t: set() for t in self.vertices}
        for e in self.edges:
            a, b = tuple(e)
            adj[a].add(b)
            adj[b].add(a)
        return adj

    def components(self) -> List[FrozenSet[Coord]]:
        """Connected components with at least one edge, as tile sets."""
        adj = self.neighbours()
        seen: Set[Coord] = set()
        out = []
        for t in self.vertices:
            if t in seen or not adj[t]:
                continue
            comp, stack = set(), [t]
            while stack:
                u = stack.pop()
                if u in comp:
                    continue
                comp.add(u)
                stack.extend(adj[u] - comp)
            seen |= comp
            out.append(frozenset(comp))
        return out

    @property
    def is_walk(self) -> bool:
        return not self.edges

    def to_dict(self) -> dict:
        return {"vertices": [[v.tile[0], v.tile[1], v.kind, v.id]
                             for v in sorted(self.vertices.values(), key=lambda v: v.tile)],
                "edges": sorted([sorted(e) for e in self.edges])}

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        s = cls()
        for r, c, kind, id in d["vertices"]:
            s.add((r, c), kind, id)
        for a, b in d["edges"]:
            s.link(tuple(a), tuple(b))
        return s


@dataclass
class Program:
    height: int
    width: int
    steps: List[Step] = field(default_factory=list)

    @property
    def area(self) -> int:
        return self.height * self.width

    @property
    def volume(self) -> int:
        return self.area * len(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    @classmethod
    def from_routes(cls, problem: Problem, mapping: Mapping, schedule: Sequence[Sequence[Route]]) -> "Program":
        """Derive the program from a static mapping and one route list per step.

        Every route is checked against its pair and the mapping. Edges follow the
        route: Z party -> first tile -> ... -> last tile -> X party or magic tile.
        """
        prog = cls(mapping.height, mapping.width)
        for routes in schedule:
            step = Step()
            for q, tile in mapping.qubits.items():
                step.add(tile, QUBIT, q)
            consumed = {r.magic: r.pair for r in routes if r.magic is not None}
            for m in mapping.magic:
                step.add(m, MAGIC, consumed.get(m, IDLE))
            for route in routes:
                pair = problem.pairs[route.pair]
                check_route(route, pair, mapping)
                tiles = route.tiles()
                for tile in tiles:
                    step.add(tile, ROUTE, route.pair)
                step.link(mapping.qubits[pair.z], tiles[0])
                for a, b in zip(tiles, tiles[1:]):
                    step.link(a, b)
                step.link(tiles[-1], route.magic if pair.is_magic else mapping.qubits[pair.x])
            prog.steps.append(step)
        return prog

    def to_dict(self) -> dict:
        return {"height": self.height, "width": self.width, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Program":
        return cls(d["height"], d["width"], [Step.from_dict(s) for s in d["steps"]])

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict()))

    @classmethod
    def load(cls, path) -> "Program":
        return cls.from_dict(json.loads(Path(path).read_text()))
