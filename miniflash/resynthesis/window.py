"""Window: a closed box cut from the tile program, the unit resynthesis works on.

A window is a box of tiles over a range of steps inside one stage (no walk step
in between, so every qubit keeps its tile), chosen so that each merge component
of those steps lies entirely inside the box or entirely outside it. Nothing
crosses its side faces: what enters and leaves is the qubit pipes through its
bottom and top faces and the magic states it consumes. A closed window therefore
realises exactly the pairs of the components it holds, in step order, on its
participant qubits, and a cell synthesised for those pairs with the same ports
can replace it.

Qubits inside the box that join no component are bystanders: they pass straight
through and stay out of the synthesis. Idle magic tiles inside the box likewise.

A slab is a step range with the closed windows that cover every component in it.
Shortening a window only shortens the program when the whole slab shortens, so
the slab is what extraction scores: its gain is bounded by the steps it spans
minus the longest dependency chain any of its windows must serialise.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from ..circuit import Problem
from ..mapping import Coord
from ..program import IDLE, MAGIC, QUBIT, ROUTE, Program, Step

LAYERS_PER_STEP = 2


@dataclass(frozen=True)
class Box:
    """Inclusive tile bounds: rows r0..r1, columns c0..c1."""
    r0: int
    c0: int
    r1: int
    c1: int

    def __post_init__(self):
        if self.r0 > self.r1 or self.c0 > self.c1:
            raise ValueError(f"empty box {self}")

    @classmethod
    def of(cls, tiles: Iterable[Coord]) -> "Box":
        tiles = list(tiles)
        if not tiles:
            raise ValueError("a box needs at least one tile")
        rows = [t[0] for t in tiles]
        cols = [t[1] for t in tiles]
        return cls(min(rows), min(cols), max(rows), max(cols))

    @property
    def height(self) -> int:
        return self.r1 - self.r0 + 1

    @property
    def width(self) -> int:
        return self.c1 - self.c0 + 1

    @property
    def area(self) -> int:
        return self.height * self.width

    def contains(self, tile: Coord) -> bool:
        return self.r0 <= tile[0] <= self.r1 and self.c0 <= tile[1] <= self.c1

    def overlaps(self, other: "Box") -> bool:
        return not (self.r1 < other.r0 or other.r1 < self.r0 or self.c1 < other.c0 or other.c1 < self.c0)

    def union(self, other: "Box") -> "Box":
        return Box(min(self.r0, other.r0), min(self.c0, other.c0), max(self.r1, other.r1), max(self.c1, other.c1))

    def tiles(self) -> List[Coord]:
        return [(r, c) for r in range(self.r0, self.r1 + 1) for c in range(self.c0, self.c1 + 1)]

    def to_dict(self) -> dict:
        return {"r0": self.r0, "c0": self.c0, "r1": self.r1, "c1": self.c1}


@dataclass(frozen=True)
class Component:
    """One merge of one step: the pair it realises, every tile it occupies (route tiles
    and both parties, the consumed magic tile included), the qubits it joins."""
    step: int
    pair: int
    tiles: FrozenSet[Coord]
    qubits: Tuple[int, ...]
    magic: Optional[Coord] = None

    @property
    def box(self) -> Box:
        return Box.of(self.tiles)

    @classmethod
    def of_step(cls, step: Step, t: int) -> List["Component"]:
        """The merge components of step t, in pair order."""
        out = []
        for comp in step.components():
            vs = [step.vertices[x] for x in comp]
            ids = {v.id for v in vs if v.kind == ROUTE} | {v.id for v in vs if v.kind == MAGIC and v.id != IDLE}
            if len(ids) != 1:
                raise ValueError(f"step {t}: component {sorted(comp)} carries pair ids {sorted(ids)}")
            (pair,) = ids
            qubits = tuple(sorted(v.id for v in vs if v.kind == QUBIT))
            magic = [v.tile for v in vs if v.kind == MAGIC]
            out.append(cls(t, pair, frozenset(comp), qubits, magic[0] if magic else None))
        return sorted(out, key=lambda c: c.pair)


@dataclass(frozen=True)
class Window:
    """A closed box over steps start..stop-1 and the components inside it.

    participants  qubits joined by some component, sorted
    bystanders    {qubit: tile} of qubits inside the box that join no component
    idle_magic    magic tiles inside the box never consumed in the range
    """
    box: Box
    start: int
    stop: int
    components: Tuple[Component, ...]
    bystanders: Dict[int, Coord]
    idle_magic: Tuple[Coord, ...]

    def __post_init__(self):
        if self.stop <= self.start:
            raise ValueError(f"window spans no step: {self.start}..{self.stop}")
        object.__setattr__(self, "components", tuple(sorted(self.components, key=lambda c: (c.step, c.pair))))
        object.__setattr__(self, "bystanders", dict(self.bystanders))
        object.__setattr__(self, "idle_magic", tuple(sorted(self.idle_magic)))

    @property
    def steps(self) -> int:
        return self.stop - self.start

    @property
    def pairs(self) -> Tuple[int, ...]:
        """Pairs in execution order: by step, then pair id."""
        return tuple(c.pair for c in self.components)

    @property
    def participants(self) -> Tuple[int, ...]:
        return tuple(sorted({q for c in self.components for q in c.qubits}))

    @property
    def magic(self) -> Tuple[Tuple[int, Coord], ...]:
        """(step, tile) of every magic state consumed, in step order."""
        return tuple((c.step, c.magic) for c in self.components if c.magic is not None)

    @property
    def ports(self) -> int:
        """Two per participant (in and out) plus one per magic state consumed."""
        return 2 * len(self.participants) + len(self.magic)

    @property
    def cubes(self) -> int:
        return self.box.area * LAYERS_PER_STEP * self.steps

    def critical_path(self, problem: Problem) -> int:
        """Longest dependency chain among the window's pairs, in pairs."""
        level: Dict[int, int] = {}
        for p in self.pairs:
            level[p] = max((level[j] + 1 for j in problem.preds[p] if j in level), default=1)
        return max(level.values(), default=0)

    def slack(self, problem: Problem) -> int:
        """Steps spanned minus the critical path: how many steps a depth-optimal cell could
        save at best, one pair per step being the floor this estimate assumes."""
        return self.steps - self.critical_path(problem)

    def check(self, program: Program) -> None:
        """Raise ValueError unless the window is closed on `program`: every component of
        its steps is inside or outside the box, its own components are exactly those inside,
        and no participant moves."""
        mine = {(c.step, c.pair) for c in self.components}
        seen = set()
        for t in range(self.start, self.stop):
            step = program.steps[t]
            for c in Component.of_step(step, t):
                inside = [self.box.contains(x) for x in c.tiles]
                if any(inside) and not all(inside):
                    raise ValueError(f"step {t}: pair {c.pair} crosses the box {self.box}")
                if all(inside):
                    if (t, c.pair) not in mine:
                        raise ValueError(f"step {t}: pair {c.pair} inside the box but not in the window")
                    seen.add((t, c.pair))
            seats = step.qubit_tiles()
            for c in self.components:
                for q in c.qubits:
                    if not self.box.contains(seats[q]):
                        raise ValueError(f"step {t}: participant q{q} at {seats[q]} outside the box")
        if seen != mine:
            raise ValueError(f"window lists pairs {sorted(mine - seen)} that its steps do not hold")

    def to_dict(self) -> dict:
        return {"box": self.box.to_dict(), "start": self.start, "stop": self.stop,
                "pairs": list(self.pairs), "participants": list(self.participants),
                "bystanders": {q: list(t) for q, t in sorted(self.bystanders.items())},
                "idle_magic": [list(m) for m in self.idle_magic],
                "magic": [[s, list(m)] for s, m in self.magic]}


@dataclass(frozen=True)
class Budget:
    """What one lassynth call may be asked to solve. None lifts a limit."""
    area: Optional[int] = 16
    steps: Optional[int] = 4
    ports: Optional[int] = 12
    cubes: Optional[int] = None

    def fits(self, window: Window) -> bool:
        return ((self.area is None or window.box.area <= self.area)
                and (self.steps is None or window.steps <= self.steps)
                and (self.ports is None or window.ports <= self.ports)
                and (self.cubes is None or window.cubes <= self.cubes))


@dataclass(frozen=True)
class Slab:
    """A step range and the closed windows that together hold every component in it."""
    start: int
    stop: int
    windows: Tuple[Window, ...]

    @property
    def steps(self) -> int:
        return self.stop - self.start

    @property
    def pairs(self) -> Tuple[int, ...]:
        return tuple(sorted(p for w in self.windows for p in w.pairs))

    def depth(self, problem: Problem) -> int:
        """Steps the slab needs at best: the longest critical path over its windows."""
        return max((w.critical_path(problem) for w in self.windows), default=0)

    def gain(self, problem: Problem) -> int:
        """Upper bound on the steps the slab can lose: its span minus its depth. Zero for
        a slab without components."""
        if not self.windows:
            return 0
        return self.steps - self.depth(problem)

    def holes(self, problem: Problem) -> int:
        """Tile-steps left empty above the windows shallower than the slab: each window's
        area times how far its critical path falls short of the slab's depth. Zero when the
        windows are balanced."""
        depth = self.depth(problem)
        return sum((depth - w.critical_path(problem)) * w.box.area for w in self.windows)

    def cuts(self, problem: Problem) -> int:
        """Dependency edges entering the slab from outside it: every one is a qubit whose
        port the cut pins between two cells."""
        mine = set(self.pairs)
        return sum(1 for p in mine for j in problem.preds[p] if j not in mine)

    def overlaps(self, other: "Slab") -> bool:
        return self.start < other.stop and other.start < self.stop

    def to_dict(self) -> dict:
        return {"start": self.start, "stop": self.stop, "windows": [w.to_dict() for w in self.windows]}
