"""Channel rearrangement as a first-class problem.

:class:`RearrangementChannel` states one inter-layer routing problem in
unified ``(row, col)`` coordinates: per-qubit :class:`Lane` records
(endpoints plus entry and exit parity), static obstacles, the available
jog planes and candidate scratch slots. :func:`solve` packs it into a
:class:`RearrangementPlan` with one of two packers — ``serial`` places
one move per track level in vacate-before-occupy order (the conservative
always-works baseline), ``dense`` packs greedy pages that use both jog
planes per row, so a level holds any interval set with point-overlap at
most two; ``best`` runs both and keeps the shallower plan. Single-row
problems yield :class:`~miniflash.floorplan.Move` records, die problems
yield :class:`~miniflash.floorplan.GridMove` records with I/J route
segments.

The packers are pure: they never mutate the problem. When a cross-row
move cannot reach any J corridor the packer raises
:class:`ChannelInfeasible` carrying the blocking slot sets per candidate
corridor, and eviction of parked qubits stays the caller's policy.

:func:`plan_blocks` renders a solved single-row channel as Cell-style
geometry blocks. Block parities follow the wire convention: each lane's
``parity_in`` rides its K and I runs, J-hops carry the flipped bit, and a
``parity_in != parity_out`` mismatch lands a hadamard on the last K unit.
Synthesized cells read parity from face colors instead, which flips at
I-K corners and holds across J-hops; the two conventions agree only on K
pins, where the worlds meet. Until they are unified, channel blocks must
not be fed to the orientation transforms, whose parity rules assume face
colors.
"""
from dataclasses import dataclass

from .floorplan import GridMove, Move


class ChannelInfeasible(RuntimeError):
    """The packer cannot realize the channel without outside help.

    ``scratch`` marks a permutation cycle with no scratch slot on offer;
    otherwise ``blocker_sets`` lists, per candidate J corridor, the static
    slots that would have to move.
    """

    def __init__(self, message, blocker_sets=(), scratch=False):
        super().__init__(message)
        self.blocker_sets = tuple(blocker_sets)
        self.scratch = scratch


@dataclass(frozen=True)
class Lane:
    """One qubit's passage through a channel: where it enters and leaves, and in which parity."""

    #: (row, col) slot where the qubit enters the channel
    start: tuple
    #: (row, col) slot where it leaves; equal to start for a pass-through lane
    end: tuple
    #: wire parity bit entering the channel
    parity_in: int = 0
    #: parity bit the consumer below expects; a mismatch lands a hadamard on the last K unit
    parity_out: int = 0


@dataclass(frozen=True)
class RearrangementChannel:
    """One channel routing problem in unified (row, col) coordinates."""

    #: {qubit: Lane}
    lanes: dict
    #: obstacle slots that may not be crossed by a J corridor
    static: frozenset = frozenset()
    #: J jog planes available to horizontal runs (the wire plane sits between them)
    planes: tuple = (0, 2)
    #: candidate scratch slots for breaking permutation cycles
    scratch: tuple = ()
    #: die width cap, or None for the single-row solver
    width: int = None


@dataclass(frozen=True)
class RearrangementPlan:
    """A solved channel: the problem, its levelled moves and the track count."""

    #: the problem this plan solves
    channel: RearrangementChannel
    #: Move (single-row) or GridMove (die) records
    moves: tuple
    #: track levels used; gap height is ``(levels + 1) * 2`` K units
    levels: int


def _sequence(raw_moves, scratch_slot):
    num_moves = len(raw_moves)
    vacater_of_slot = {source: index for index, (_, source, _) in enumerate(raw_moves)}
    successor = {}
    for index, (_, _, destination) in enumerate(raw_moves):
        vacater = vacater_of_slot.get(destination)
        if vacater is not None and vacater != index:
            successor[vacater] = index
    has_predecessor = set(successor.values())

    ordered = []
    visited = set()
    for start in range(num_moves):
        if start in visited or start in has_predecessor:
            continue
        index = start
        while index is not None and index not in visited:
            visited.add(index)
            ordered.append(raw_moves[index])
            index = successor.get(index)

    for start in range(num_moves):
        if start in visited:
            continue
        if scratch_slot is None:
            raise ChannelInfeasible("solve: permutation cycle needs a scratch slot", scratch=True)
        cycle = []
        index = start
        while index not in visited:
            visited.add(index)
            cycle.append(index)
            index = successor[index]
        qubit, source, destination = raw_moves[start]
        ordered.append((qubit, source, scratch_slot))
        for member in cycle[1:]:
            ordered.append(raw_moves[member])
        ordered.append((qubit, scratch_slot, destination))

    return ordered


def _predecessors(ordered):
    vacater_of_slot = {}
    predecessors = []
    for index, (_, source, destination) in enumerate(ordered):
        predecessors.append(vacater_of_slot.get(destination))
        vacater_of_slot[source] = index
    return predecessors


def _chain_depths(predecessors):
    successors = {}
    for index, predecessor in enumerate(predecessors):
        if predecessor is not None:
            successors[predecessor] = index
    depths = [0] * len(predecessors)
    for leaf in (index for index in range(len(predecessors)) if index not in successors):
        depth = 0
        current = leaf
        while current is not None:
            depths[current] = max(depths[current], depth)
            current = predecessors[current]
            depth += 1
    return depths


class _Level:
    def __init__(self):
        self.intervals = {}
        self.corridors = []

    def interval_fits(self, row, plane, low, high):
        for other_low, other_high in self.intervals.get((row, plane), ()):
            if not (high < other_low or other_high < low):
                return False
        for column, row_low, row_high in self.corridors:
            if low <= column <= high and row_low <= row <= row_high:
                return False
        return True

    def corridor_fits(self, column, row_low, row_high):
        for other_column, other_low, other_high in self.corridors:
            if column == other_column and not (row_high < other_low or other_high < row_low):
                return False
        for (row, _), spans in self.intervals.items():
            if row_low <= row <= row_high and any(low <= column <= high for low, high in spans):
                return False
        return True

    def add_interval(self, row, plane, low, high):
        self.intervals.setdefault((row, plane), []).append((low, high))

    def add_corridor(self, column, row_low, row_high):
        self.corridors.append((column, row_low, row_high))


def _plane_order(channel, level, row, from_column, to_column, dense):
    if not dense:
        return (channel.planes[-1] if to_column > from_column else channel.planes[0],)
    return tuple(sorted(channel.planes, key=lambda plane: len(level.intervals.get((row, plane), ()))))


def _try_intra_row(channel, level, move, dense):
    _, (row, from_column), (_, to_column) = move
    low, high = min(from_column, to_column), max(from_column, to_column)
    for plane in _plane_order(channel, level, row, from_column, to_column, dense):
        if level.interval_fits(row, plane, low, high):
            return [("I", row, plane, low, high)]
    return None


def _corridor_candidates(channel, to_column):
    for delta in range(channel.width):
        for corridor in (to_column + delta, to_column - delta):
            if 0 <= corridor < channel.width:
                yield corridor


def _corridor_blockers(channel, corridor, row_low, row_high):
    return frozenset(slot for slot in ((row, corridor) for row in range(row_low, row_high + 1)) if slot in channel.static)


def _try_cross_row(channel, level, move, dense):
    _, (from_row, from_column), (to_row, to_column) = move
    row_low, row_high = min(from_row, to_row), max(from_row, to_row)
    for corridor in _corridor_candidates(channel, to_column):
        if _corridor_blockers(channel, corridor, row_low, row_high):
            continue
        if not level.corridor_fits(corridor, row_low, row_high):
            continue
        segments = []
        feasible = True
        for row, near, far in ((from_row, from_column, corridor), (to_row, corridor, to_column)):
            if near == far:
                continue
            low, high = min(near, far), max(near, far)
            for plane in _plane_order(channel, level, row, near, far, dense):
                if level.interval_fits(row, plane, low, high):
                    segments.append(("I", row, plane, low, high))
                    break
            else:
                feasible = False
                break
        if not feasible:
            continue
        if len(segments) == 2:
            segments.insert(1, ("J", corridor, row_low, row_high))
        elif from_column == corridor:
            segments.insert(0, ("J", corridor, row_low, row_high))
        else:
            segments.append(("J", corridor, row_low, row_high))
        return corridor, segments
    return None


def _place(channel, level, move, dense):
    _, (from_row, _), (to_row, _) = move
    if from_row == to_row:
        segments = _try_intra_row(channel, level, move, dense)
        if segments is None:
            return None
        _, row, plane, low, high = segments[0]
        level.add_interval(row, plane, low, high)
        return segments
    placement = _try_cross_row(channel, level, move, dense)
    if placement is None:
        return None
    corridor, segments = placement
    row_low, row_high = min(from_row, to_row), max(from_row, to_row)
    level.add_corridor(corridor, row_low, row_high)
    for segment in segments:
        if segment[0] == "I":
            level.add_interval(segment[1], segment[2], segment[3], segment[4])
    return segments


def _diagnose(channel, move):
    _, (from_row, _), (to_row, to_column) = move
    row_low, row_high = min(from_row, to_row), max(from_row, to_row)
    blocker_sets = [_corridor_blockers(channel, corridor, row_low, row_high) for corridor in _corridor_candidates(channel, to_column)]
    raise ChannelInfeasible(f"solve: no clear J corridor between rows {from_row} and {to_row} within width {channel.width}", blocker_sets=blocker_sets)


def _pack(channel, dense):
    movers = [(qubit, lane.start, lane.end) for qubit, lane in sorted(channel.lanes.items()) if lane.start != lane.end]
    scratch_slot = channel.scratch[0] if channel.scratch else None
    if channel.width is None and scratch_slot is None and movers:
        columns = [slot[1] for _, start, end in movers for slot in (start, end)]
        scratch_slot = (0, max(columns) + 1)
    ordered = _sequence(movers, scratch_slot)
    predecessors = _predecessors(ordered)
    depths = _chain_depths(predecessors)

    placed_level = {}
    segments_of = {}
    pending = list(range(len(ordered)))
    level_index = 0
    while pending:
        level = _Level()
        ready = [index for index in pending if predecessors[index] is None or placed_level.get(predecessors[index], level_index) < level_index]
        ready.sort(key=lambda index: (-depths[index], -abs(ordered[index][2][1] - ordered[index][1][1]), index))
        placed_any = False
        for index in ready:
            segments = _place(channel, level, ordered[index], dense)
            if segments is None:
                continue
            placed_level[index] = level_index
            segments_of[index] = segments
            placed_any = True
            if not dense:
                break
        if not placed_any:
            _diagnose(channel, ordered[ready[0]] if ready else ordered[pending[0]])
        pending = [index for index in pending if index not in placed_level]
        level_index += 1

    moves = []
    for index, (qubit, source, destination) in enumerate(ordered):
        level = placed_level[index]
        if channel.width is None:
            plane = next(segment[2] for segment in segments_of[index] if segment[0] == "I")
            moves.append(Move(qubit=qubit, from_column=source[1], to_column=destination[1], level=level, plane=plane))
        else:
            moves.append(GridMove(qubit=qubit, src=source, dst=destination, level=level, path=tuple(segments_of[index])))
    return RearrangementPlan(channel=channel, moves=tuple(moves), levels=level_index)


def solve(channel, packer="best") -> RearrangementPlan:
    """Pack a RearrangementChannel into levelled moves.

    :param channel: RearrangementChannel.
    :param packer: "serial" (one move per level, vacate order), "dense"
        (greedy pages over both jog planes) or "best" (both, keep the
        shallower plan).
    :returns: RearrangementPlan.
    :raises ChannelInfeasible: on a cycle without scratch or a cross-row
        move with every corridor blocked (``blocker_sets`` names the
        static slots per candidate).
    """
    if packer == "serial":
        return _pack(channel, dense=False)
    if packer == "dense":
        return _pack(channel, dense=True)
    dense_plan = _pack(channel, dense=True)
    serial_plan = _pack(channel, dense=False)
    return dense_plan if dense_plan.levels <= serial_plan.levels else serial_plan


def check_plan(plan) -> list:
    """Independently verify a RearrangementPlan.

    Replays the plan level by level and reports conflicts the packers must
    never produce: same-level same-plane interval overlaps, corridor
    collisions and static crossings, vacate-before-occupy violations, and
    a final position map that differs from the requested lanes.

    :param plan: RearrangementPlan.
    :returns: list[str] problems (empty = valid).
    """
    problems = []
    channel = plan.channel
    by_level = {}
    for move in plan.moves:
        by_level.setdefault(move.level, []).append(move)

    def endpoints(move):
        if isinstance(move, GridMove):
            return move.src, move.dst
        return (0, move.from_column), (0, move.to_column)

    def segments(move):
        if isinstance(move, GridMove):
            return move.path
        low, high = min(move.from_column, move.to_column), max(move.from_column, move.to_column)
        return (("I", 0, move.plane, low, high),)

    for level_index, level_moves in sorted(by_level.items()):
        level = _Level()
        for move in level_moves:
            for segment in segments(move):
                if segment[0] == "I":
                    _, row, plane, low, high = segment
                    if not level.interval_fits(row, plane, low, high):
                        problems.append(f"level {level_index}: interval conflict at row {row} plane {plane} [{low},{high}]")
                    level.add_interval(row, plane, low, high)
                else:
                    _, column, row_low, row_high = segment
                    if _corridor_blockers(channel, column, row_low, row_high):
                        problems.append(f"level {level_index}: corridor {column} crosses static slots")
                    if not level.corridor_fits(column, row_low, row_high):
                        problems.append(f"level {level_index}: corridor conflict at column {column}")
                    level.add_corridor(column, row_low, row_high)

    position = {qubit: lane.start for qubit, lane in channel.lanes.items()}
    occupied = {lane.start for lane in channel.lanes.values()} | set(channel.static)
    for level_index, level_moves in sorted(by_level.items()):
        for move in level_moves:
            source, _ = endpoints(move)
            if position.get(move.qubit) != source:
                problems.append(f"level {level_index}: qubit {move.qubit} moves from {source} but sits at {position.get(move.qubit)}")
            occupied.discard(source)
        for move in level_moves:
            _, destination = endpoints(move)
            if destination in occupied:
                problems.append(f"level {level_index}: qubit {move.qubit} lands on occupied slot {destination}")
            position[move.qubit] = destination
            occupied.add(destination)
    for qubit, lane in channel.lanes.items():
        if position[qubit] != lane.end:
            problems.append(f"qubit {qubit} ends at {position[qubit]} instead of {lane.end}")
    return problems


def _k_run(blocks, column, k_start, k_end, parity):
    for k in range(k_start + 1, k_end, 2):
        blocks.append({"kind": "pipe", "pos": [2 * column, 2, k], "axis": "K", "parity": parity})


def plan_blocks(plan, height):
    """Render a solved single-row channel as Cell-style blocks.

    Local coordinates: i = 2×column, the wire plane at j=2 with jog planes
    at j=0/4 (hops crossing j=1/3), k from 0 (below the upper layer) to
    ``height``. Parities follow the wire convention — each lane's
    ``parity_in`` rides its K and I runs, J-hops carry the flipped bit,
    and a ``parity_in != parity_out`` mismatch lands a hadamard on the
    last K unit before the consuming cell.

    :param plan: RearrangementPlan over row-0 lanes.
    :param height: int, gap height in K units (even).
    :returns: list of block dicts, emittable by ``emit_cell_pipes``.
    """
    by_qubit = {}
    for move in plan.moves:
        by_qubit.setdefault(move.qubit, []).append(move)

    blocks = []
    for qubit, lane in sorted(plan.channel.lanes.items()):
        parity = lane.parity_in
        hop_parity = 1 - parity
        column = lane.start[1]
        k = 0
        for move in sorted(by_qubit.get(qubit, []), key=lambda move: move.level):
            jog = (move.level + 1) * 2
            _k_run(blocks, column, k, jog, parity)
            run_j = move.plane * 2
            hop_j = 1 if move.plane == 0 else 3
            blocks.append({"kind": "pipe", "pos": [2 * column, hop_j, jog], "axis": "J", "parity": hop_parity})
            step = 2 if move.to_column > column else -2
            for i in range(2 * column, 2 * move.to_column, step):
                blocks.append({"kind": "pipe", "pos": [i + step // 2, run_j, jog], "axis": "I", "parity": parity})
            blocks.append({"kind": "pipe", "pos": [2 * move.to_column, hop_j, jog], "axis": "J", "parity": hop_parity})
            column, k = move.to_column, jog
        if lane.parity_in != lane.parity_out:
            _k_run(blocks, column, k, height - 2, parity)
            blocks.append({"kind": "hadamard", "pos": [2 * column, 2, height - 1], "axis": "K", "parity": parity})
        else:
            _k_run(blocks, column, k, height, parity)
    return blocks
