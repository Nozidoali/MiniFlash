"""Magic-state supply geometry: factories, delivery rings, and one path per consumed state.

The chip is the program's H x W grid. With K factories it is wrapped in K rings of
tiles, ring i (1 = innermost) running along rows -i and H - 1 + i and columns -i and
W - 1 + i, so the whole area is (H + 2K) x (W + 2K); each ring belongs to one
factory. Factories sit outside the outermost ring, placed clockwise from the
top-left corner (top side left to right, then the right side downward, and so on),
one tile apart. Each factory has one port, the middle tile of its face towards the
rings. A port near a corner lies beyond the ends of the inner rings, so its
straight line in meets only the outer ones: rings are dealt out so that each
factory gets one its line meets, the factories nearest the corners taking the
outermost rings.

A consumed magic state at step t travels from its factory's port straight in to that
factory's ring (crossing the outer rings), around the ring the short way to the magic
tile's row on the magic tile's side of the chip, then straight in along that row to
the magic tile, crossing the inner rings and any chip tiles the step leaves free. The
k-th state consumed goes to factory k mod K. `plan` records every tile two
deliveries share within a step, and every chip tile a delivery would cross that the
step uses, in `Supply.conflicts`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import List, Tuple

from .factory import Factory
from .mapping import Coord
from .program import IDLE, MAGIC, Program


@dataclass(frozen=True)
class Delivery:
    step: int
    factory: int
    magic: Coord
    tiles: Tuple[Coord, ...]  # port, ..., magic tile, in walking order


@dataclass(frozen=True)
class Box:
    side: str                 # "top" | "right" | "bottom" | "left"
    row: int                  # top-left tile
    col: int
    height: int
    width: int
    port: Coord               # middle tile of the face towards the rings
    ring: int = 0             # the ring this factory delivers on (1 = innermost)


@dataclass
class Supply:
    rings: int
    factory: Factory
    boxes: List[Box] = field(default_factory=list)
    deliveries: List[Delivery] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)


def ring_tiles(i: int, height: int, width: int) -> List[Coord]:
    """Ring i clockwise from its top-left corner."""
    top, bottom, left, right = -i, height - 1 + i, -i, width - 1 + i
    out = [(top, c) for c in range(left, right + 1)]
    out += [(r, right) for r in range(top + 1, bottom + 1)]
    out += [(bottom, c) for c in range(right - 1, left - 1, -1)]
    out += [(r, left) for r in range(bottom - 1, top, -1)]
    return out


def place_factories(count: int, height: int, width: int, factory: Factory) -> List[Box]:
    """Factories clockwise outside ring `count`, one tile apart. A factory is
    `factory.height` tiles deep (away from the chip) and `factory.width` along its side."""
    k, depth, span = count, factory.height, factory.width
    boxes: List[Box] = []
    for side, length in (("top", width + 2 * k), ("right", height + 2 * k), ("bottom", width + 2 * k), ("left", height + 2 * k)):
        s = 0
        while len(boxes) < count and s + span <= length:
            if side == "top":
                r, c = -k - depth, -k + s
                boxes.append(Box(side, r, c, depth, span, (r + depth - 1, c + span // 2)))
            elif side == "right":
                r, c = -k + s, width + k
                boxes.append(Box(side, r, c, span, depth, (r + span // 2, c)))
            elif side == "bottom":
                r, c = height + k, width + k - s - span
                boxes.append(Box(side, r, c, depth, span, (r, c + span // 2)))
            else:
                r, c = height + k - s - span, -k - depth
                boxes.append(Box(side, r, c, span, depth, (r + span // 2, c + depth - 1)))
            s += span + 1
    if len(boxes) < count:
        raise ValueError(f"{count} factories of {depth}x{span} do not fit around a {height}x{width} chip")
    return assign_rings(boxes, height, width)


def innermost_ring(box: Box, height: int, width: int) -> int:
    """The innermost ring the port's straight line in meets."""
    r, c = box.port
    if box.side in ("top", "bottom"):
        return max(1, -c, c - (width - 1))
    return max(1, -r, r - (height - 1))


def assign_rings(boxes: List[Box], height: int, width: int) -> List[Box]:
    """Rings K, K-1, ..., 1 to the factories whose lines meet the fewest rings first."""
    k = len(boxes)
    order = sorted(range(k), key=lambda f: (-innermost_ring(boxes[f], height, width), f))
    out = list(boxes)
    for ring, f in zip(range(k, 0, -1), order):
        need = innermost_ring(boxes[f], height, width)
        if need > ring:
            raise ValueError(f"factory {f}'s port {boxes[f].port} meets no ring inside ring {need}")
        out[f] = replace(boxes[f], ring=ring)
    return out


def _inward(box: Box, ring: int, height: int, width: int) -> List[Coord]:
    """From the port straight in to ring `ring`, crossing the rings outside it."""
    r, c = box.port
    if box.side == "top":
        return [(rr, c) for rr in range(r + 1, -ring + 1)]
    if box.side == "bottom":
        return [(rr, c) for rr in range(r - 1, height - 2 + ring, -1)]
    if box.side == "left":
        return [(r, cc) for cc in range(c + 1, -ring + 1)]
    return [(r, cc) for cc in range(c - 1, width - 2 + ring, -1)]


def _around(ring: List[Coord], a: Coord, b: Coord) -> List[Coord]:
    """Tiles of the ring from a to b the short way, both included."""
    i, j = ring.index(a), ring.index(b)
    n = len(ring)
    forward = [ring[(i + s) % n] for s in range((j - i) % n + 1)]
    backward = [ring[(i - s) % n] for s in range((i - j) % n + 1)]
    return forward if len(forward) <= len(backward) else backward


def delivery_path(box: Box, ring: int, magic: Coord, height: int, width: int) -> List[Coord]:
    """Port -> ring -> around -> along the magic tile's row -> magic tile."""
    r, c = magic
    left = c < width / 2
    exit_tile = (r, -ring) if left else (r, width - 1 + ring)
    radial = _inward(box, ring, height, width)
    tiles = ring_tiles(ring, height, width)
    around = _around(tiles, radial[-1], exit_tile)
    inward = [(r, cc) for cc in range(-ring + 1, c)] if left else [(r, cc) for cc in range(width - 2 + ring, c, -1)]
    return [box.port] + radial[:-1] + around + inward + [magic]


def plan(program: Program, factories: int, factory: Factory = Factory()) -> Supply:
    """Rings, factory boxes and one delivery per consumed magic state."""
    height, width = program.height, program.width
    supply = Supply(factories, factory, place_factories(factories, height, width, factory))
    k = 0
    for t, step in enumerate(program.steps):
        used = {}
        for v in sorted(step.of_kind(MAGIC), key=lambda v: v.tile):
            if v.id == IDLE:
                continue
            f = k % factories
            k += 1
            path = delivery_path(supply.boxes[f], supply.boxes[f].ring, v.tile, height, width)
            supply.deliveries.append(Delivery(t, f, v.tile, tuple(path)))
            for tile in path:
                if tile in used:
                    supply.conflicts.append(f"step {t}: factories {used[tile]} and {f} both use {tile}")
                used[tile] = f
                inside = 0 <= tile[0] < height and 0 <= tile[1] < width
                if inside and tile != v.tile and tile in step.vertices:
                    supply.conflicts.append(f"step {t}: factory {f} crosses {tile}, used by the chip")
    return supply
