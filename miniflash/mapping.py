"""Mapping: the grid, where each qubit sits, and where the magic tiles are.

Coordinates are (row, col) with row 0 at the top. Every qubit faces the same way:
its Z boundary is its top and bottom side and its X boundary its left and right
side. A Mapping is the first intermediate object a solver produces; together with the
routes it derives the Program.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

Coord = Tuple[int, int]


def _paired_positions(k: int) -> Tuple[list, int]:
    """Site coordinates 1,2,4,5,7,8,... and the parity-dependent array extent."""
    pos, cur = [], 1
    for i in range(k):
        pos.append(cur)
        cur += 1 if i % 2 == 0 else 2
    return pos, cur


@dataclass(frozen=True)
class Mapping:
    height: int
    width: int
    qubits: Dict[int, Coord]
    magic: Tuple[Coord, ...] = ()
    _occupied: FrozenSet[Coord] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "qubits", dict(self.qubits))
        object.__setattr__(self, "magic", tuple(tuple(m) for m in self.magic))
        tiles = list(self.qubits.values()) + list(self.magic)
        for t in tiles:
            if not self.in_bounds(t):
                raise ValueError(f"tile {t} outside the {self.height}x{self.width} grid")
        if len(set(tiles)) != len(tiles):
            raise ValueError("two qubits or magic tiles share a tile")
        object.__setattr__(self, "_occupied", frozenset(tiles))

    def in_bounds(self, t: Coord) -> bool:
        return 0 <= t[0] < self.height and 0 <= t[1] < self.width

    def is_free(self, t: Coord) -> bool:
        return self.in_bounds(t) and t not in self._occupied

    @property
    def occupied(self) -> FrozenSet[Coord]:
        return self._occupied

    def vertical(self, t: Coord) -> Tuple[Coord, ...]:
        """Tiles above and below t."""
        r, c = t
        return tuple(x for x in ((r - 1, c), (r + 1, c)) if self.in_bounds(x))

    def horizontal(self, t: Coord) -> Tuple[Coord, ...]:
        """Tiles left and right of t."""
        r, c = t
        return tuple(x for x in ((r, c - 1), (r, c + 1)) if self.in_bounds(x))

    def z_side(self, q: int) -> Tuple[Coord, ...]:
        """Tiles adjacent to qubit q's Z boundary: above and below."""
        return self.vertical(self.qubits[q])

    def x_side(self, q: int) -> Tuple[Coord, ...]:
        """Tiles adjacent to qubit q's X boundary: left and right."""
        return self.horizontal(self.qubits[q])

    def magic_side(self, m: Coord) -> Tuple[Coord, ...]:
        """A magic state is prepared in whatever orientation suits: any neighbour attaches."""
        return self.vertical(m) + self.horizontal(m)

    @classmethod
    def sparse(cls, site_rows: int, site_cols: int, n_qubits: int, order: Iterable[int] = None) -> "Mapping":
        """DasCot's sparse array: site_rows x site_cols sites at even coordinates starting
        at 2, one free tile between neighbours, a padding ring, and a magic tile on
        every other tile of the ring. Sites fill row-major in `order` (qubit index by
        default); the trailing sites stay empty. The reference solvers place here."""
        if site_rows * site_cols < n_qubits:
            raise ValueError(f"{site_rows}x{site_cols} sites cannot hold {n_qubits} qubits")
        h, w = 2 * site_rows + 3, 2 * site_cols + 3
        sites = [(r, c) for r in range(2, h - 2, 2) for c in range(2, w - 2, 2)]
        order = list(order) if order is not None else list(range(n_qubits))
        qubits = {q: sites[i] for i, q in enumerate(order)}
        tiles = ([(0, c) for c in range(1, w, 2)] + [(r, w - 1) for r in range(1, h, 2)]
                 + [(h - 1, c) for c in range(1, w, 2)] + [(r, 0) for r in range(1, h, 2)])
        return cls(h, w, qubits, tuple(dict.fromkeys(tiles)))

    @classmethod
    def paired(cls, site_rows: int, site_cols: int, n_qubits: int, order: Iterable[int] = None) -> "Mapping":
        """Paired-lane array: site coordinates 1,2,4,5,7,8,... so every two site rows
        share one lane row and every two site columns one lane column, with a lane
        before the first site and no padding ring. The final extent depends on parity. A magic tile
        sits at both ends of every site row."""
        if site_rows * site_cols < n_qubits:
            raise ValueError(f"{site_rows}x{site_cols} sites cannot hold {n_qubits} qubits")
        rows, h = _paired_positions(site_rows)
        cols, wq = _paired_positions(site_cols)
        if site_cols % 2 == 1:
            wq += 1
        cols = [c + 1 for c in cols]
        w = wq + 2
        sites = [(r, c) for r in rows for c in cols]
        order = list(order) if order is not None else list(range(n_qubits))
        qubits = {q: sites[i] for i, q in enumerate(order)}
        return cls(h, w, qubits, tuple((r, c) for r in rows for c in (0, w - 1)))


# Layout: the virtual map behind a Mapping.
# 
# A Layout fixes the qubit order as virtual sites (i, j) on a site array, nothing
# else. Coordinates come from the array geometry and
# from compaction, which keeps this order and drops unused lanes.

@dataclass(frozen=True)
class Layout:
    site_rows: int
    site_cols: int
    order: Dict[int, Tuple[int, int]]  # qubit -> (i, j) virtual site

    def __post_init__(self):
        object.__setattr__(self, "order", {q: tuple(s) for q, s in self.order.items()})
        sites = list(self.order.values())
        if len(set(sites)) != len(sites):
            raise ValueError("two qubits share a virtual site")
        for i, j in sites:
            if not (0 <= i < self.site_rows and 0 <= j < self.site_cols):
                raise ValueError(f"virtual site {(i, j)} outside {self.site_rows}x{self.site_cols}")

    @classmethod
    def identity(cls, n_qubits: int, shape: Optional[Tuple[int, int]] = None) -> "Layout":
        """Qubit q at site (q // b, q % b) on an a x b site array, the smallest square by default."""
        a, b = shape if shape is not None else (math.ceil(math.sqrt(n_qubits)),) * 2
        return cls(a, b, {q: (q // b, q % b) for q in range(n_qubits)})
