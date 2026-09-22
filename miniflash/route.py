"""Route: the second intermediate object, one per pair per step, and the candidate search.

A route is a chain of straight segments through free tiles. Consecutive segments
share their corner tile: segment i ends where segment i+1 starts. A straight route
has one segment and an L has two. The shared search also tries three-segment
detours. The qubit tiles are never part of the route: the first tile must be a vertical neighbour of
the Z party and the last tile a horizontal neighbour of the X party, which is a
qubit or the consumed magic tile. A magic tile attaches from any side.
`free_routes` enumerates the straight, L, and three-segment routes between two
parties' sides; `shortest_route` and `shortest_magic_route` pick the shortest.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from .circuit import Pair, Problem
from .mapping import Coord, Mapping


class RouteError(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    start: Coord
    end: Coord

    def __post_init__(self):
        object.__setattr__(self, "start", tuple(self.start))
        object.__setattr__(self, "end", tuple(self.end))
        if self.start[0] != self.end[0] and self.start[1] != self.end[1]:
            raise RouteError(f"segment {self.start}->{self.end} is not straight")

    @property
    def length(self) -> int:
        return abs(self.end[0] - self.start[0]) + abs(self.end[1] - self.start[1]) + 1

    def tiles(self) -> List[Coord]:
        (r0, c0), (r1, c1) = self.start, self.end
        if r0 == r1:
            step = 1 if c1 >= c0 else -1
            return [(r0, c) for c in range(c0, c1 + step, step)]
        step = 1 if r1 >= r0 else -1
        return [(r, c0) for r in range(r0, r1 + step, step)]


@dataclass(frozen=True)
class Route:
    pair: int
    segments: Tuple[Segment, ...]
    magic: Optional[Coord] = None  # the magic tile consumed, for (q, MAGIC) pairs

    def __post_init__(self):
        object.__setattr__(self, "segments", tuple(self.segments))
        if self.magic is not None:
            object.__setattr__(self, "magic", tuple(self.magic))
        if not self.segments:
            raise RouteError(f"route for pair {self.pair} has no segments")
        for a, b in zip(self.segments, self.segments[1:]):
            if a.end != b.start:
                raise RouteError(f"route for pair {self.pair}: segment ends at {a.end}, next starts at {b.start}")

    @property
    def start(self) -> Coord:
        return self.segments[0].start

    @property
    def end(self) -> Coord:
        return self.segments[-1].end

    def tiles(self) -> List[Coord]:
        """Tiles in walking order, the shared corner listed once."""
        out = list(self.segments[0].tiles())
        for s in self.segments[1:]:
            out.extend(s.tiles()[1:])
        return out

    @property
    def length(self) -> int:
        return len(self.tiles())


def check_route(route: Route, pair: Pair, mapping: Mapping) -> None:
    """Raise RouteError unless `route` legally realises `pair` on `mapping`."""
    tiles = route.tiles()
    if len(set(tiles)) != len(tiles):
        raise RouteError(f"pair {route.pair}: route revisits a tile")
    for t in tiles:
        if not mapping.is_free(t):
            raise RouteError(f"pair {route.pair}: tile {t} is not a free tile")
    if route.start not in mapping.z_side(pair.z):
        raise RouteError(f"pair {route.pair}: start {route.start} is not on the Z side of q{pair.z} at {mapping.qubits[pair.z]}")
    if pair.is_magic:
        if route.magic is None:
            raise RouteError(f"pair {route.pair}: T route names no magic tile")
        if route.magic not in mapping.magic:
            raise RouteError(f"pair {route.pair}: {route.magic} is not a magic tile")
        if route.end not in mapping.magic_side(route.magic):
            raise RouteError(f"pair {route.pair}: end {route.end} is not adjacent to magic {route.magic}")
    else:
        if route.magic is not None:
            raise RouteError(f"pair {route.pair}: CX route consumes a magic tile")
        if route.end not in mapping.x_side(pair.x):
            raise RouteError(f"pair {route.pair}: end {route.end} is not on the X side of q{pair.x} at {mapping.qubits[pair.x]}")


def segments_of(tiles: Sequence[Coord]) -> Tuple[Segment, ...]:
    """Straight segments of a tile path, one per maximal straight run."""
    if not tiles:
        raise RouteError("empty path")
    segs = []
    start = tiles[0]
    for a, b, c in zip(tiles, tiles[1:], tiles[2:]):
        if (b[0] - a[0], b[1] - a[1]) != (c[0] - b[0], c[1] - b[1]):
            segs.append(Segment(start, b))
            start = b
    segs.append(Segment(start, tiles[-1]))
    return tuple(segs)


def l_routes(start: Coord, end: Coord) -> List[Tuple[Segment, ...]]:
    """The straight or two L segment chains from start to end (row-first, then column-first)."""
    (r0, c0), (r1, c1) = start, end
    if r0 == r1 or c0 == c1:
        return [(Segment(start, end),)]
    return [(Segment(start, (r0, c1)), Segment((r0, c1), end)),
            (Segment(start, (r1, c0)), Segment((r1, c0), end))]


Schedule = List[List[Route]]


def free_routes(pair_id: int, starts: Sequence[Coord], ends: Sequence[Coord], mapping: Mapping,
                magic: Optional[Coord] = None, blocked: Sequence[Coord] = (), connectors: int = 3) -> List[Route]:
    """Generate free straight, L, and limited three-segment routes from `starts` to
    `ends`. Three-segment routes run start line -> connecting line -> end line through a
    connecting column (row -> column -> row) or row, tried only for start/end pairs that
    no straight or L route joins; the `connectors` nearest lines that give a free route
    are kept, nearest meaning inside the span between start and end first, then by
    distance from it. This covers parallel lines and a shared line whose direct segment
    is blocked (a U-turn)."""
    blocked = set(blocked)
    out = []

    def free(tiles):
        return all(mapping.is_free(t) and t not in blocked for t in tiles)

    for st in starts:
        if not mapping.is_free(st) or st in blocked:
            continue
        for en in ends:
            if not mapping.is_free(en) or en in blocked:
                continue
            direct = 0
            for segs in l_routes(st, en):
                if free([t for seg in segs for t in seg.tiles()]):
                    out.append(Route(pair_id, segs, magic))
                    direct += 1
            if direct:
                continue
            # no straight or L route: row -> column -> row via a connecting column c, and
            # column -> row -> column via a row r; on a shared line this is the U-turn
            lo, hi = sorted((st[1], en[1]))
            found = 0
            for c in sorted(range(mapping.width), key=lambda c: (0 if lo < c < hi else min(abs(c - lo), abs(c - hi)), c)):
                if c in (st[1], en[1]):
                    continue
                segs = (Segment(st, (st[0], c)), Segment((st[0], c), (en[0], c)), Segment((en[0], c), en))
                if free([t for seg in segs for t in seg.tiles()]):
                    out.append(Route(pair_id, segs, magic))
                    found += 1
                    if found == connectors:
                        break
            lo, hi = sorted((st[0], en[0]))
            found = 0
            for r in sorted(range(mapping.height), key=lambda r: (0 if lo < r < hi else min(abs(r - lo), abs(r - hi)), r)):
                if r in (st[0], en[0]):
                    continue
                segs = (Segment(st, (r, st[1])), Segment((r, st[1]), (r, en[1])), Segment((r, en[1]), en))
                if free([t for seg in segs for t in seg.tiles()]):
                    out.append(Route(pair_id, segs, magic))
                    found += 1
                    if found == connectors:
                        break
    return out


def shortest_route(pair_id: int, z: int, x: int, mapping: Mapping, blocked: Sequence[Coord] = ()) -> Optional[Route]:
    """Shortest free route from qubit z's Z side to qubit x's X side,
    or None; ties keep enumeration order."""
    best = None
    for cand in free_routes(pair_id, mapping.z_side(z), mapping.x_side(x), mapping, blocked=blocked):
        if best is None or cand.length < best.length:
            best = cand
    return best


def shortest_magic_route(pair_id: int, z: int, mapping: Mapping, blocked: Sequence[Coord] = (),
                         consumed: Sequence[Coord] = ()) -> Optional[Route]:
    """Shortest free route from qubit z's Z side to any side of a magic tile not in
    `consumed`. Magic tiles are tried by distance and the search stops once no closer
    tile can beat the best route found."""
    z_tile = mapping.qubits[z]

    def dist(m):
        return abs(m[0] - z_tile[0]) + abs(m[1] - z_tile[1])
    consumed = set(consumed)
    best = None
    for m in sorted(mapping.magic, key=lambda m: (dist(m), m)):
        if best is not None and dist(m) - 1 >= best.length:
            break
        if m in consumed:
            continue
        for cand in free_routes(pair_id, mapping.z_side(z), mapping.magic_side(m), mapping, magic=m, blocked=blocked):
            if best is None or cand.length < best.length:
                best = cand
    return best
