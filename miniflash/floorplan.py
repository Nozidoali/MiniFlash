"""The symbolic floorplan.

Decides *where everything will be* before any geometry exists: parking
lanes, port columns and orders, channel moves and gap levels. Cells are
synthesized afterwards to match these decisions, which is what makes
routing retry-free — the wire never adapts to the cell; the cell adapts
to the wire.

This module holds the shared move types (:class:`Move`,
:class:`SideMove`, :class:`GridMove`), the packing helpers, the
:func:`solve_floorplan` dispatcher and injection placement
(:func:`place_injections`). The concrete solvers live in
:mod:`miniflash.solver1d` (column-stable 1-D) and
:mod:`miniflash.solver2d` (slot-stable die mode).
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Move:
    qubit: int
    from_column: int
    to_column: int
    level: int
    plane: int


@dataclass(frozen=True)
class SideMove:
    qubit: int
    lane_column: int
    slot: int
    plane: int


@dataclass
class Floorplan:
    num_qubits: int
    box_widths: list
    in_port_columns: list
    out_port_columns: list
    lane_columns: list
    moves: list
    gap_levels: list = field(default_factory=list)
    side_exits: list = field(default_factory=list)
    side_entries: list = field(default_factory=list)
    magic_column: int = None
    die_dims: tuple = None
    rows: int = 0
    placements: list = field(default_factory=list)
    parked_slots: list = field(default_factory=list)
    grid_moves: list = field(default_factory=list)


@dataclass(frozen=True)
class GridMove:
    qubit: int
    src: tuple
    dst: tuple
    level: int
    path: tuple


INJECTION_BANDWIDTH = 2


@dataclass(frozen=True)
class InjectionPoint:
    qubit: int
    dagger: bool
    channel: int
    column: int
    row: int = 0
    round: int = 0
    slot: int = 0
    level: int = -1


def sequence_moves(raw_moves, scratch_column):
    """Order channel moves vacate-before-occupy; break permutation cycles via the scratch column.

    :param raw_moves: list of (qubit, from_column, to_column).
    :param scratch_column: int, spare column for cycle breaking.
    :returns: ordered move list (cycle members routed through scratch).
    """
    num_moves = len(raw_moves)
    vacater_of_column = {from_column: index for index, (_, from_column, _) in enumerate(raw_moves)}
    successor = {}
    for index, (_, _, to_column) in enumerate(raw_moves):
        vacater = vacater_of_column.get(to_column)
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
        cycle = []
        index = start
        while index not in visited:
            visited.add(index)
            cycle.append(index)
            index = successor[index]
        qubit, from_column, to_column = raw_moves[start]
        ordered.append((qubit, from_column, scratch_column))
        for member in cycle[1:]:
            ordered.append(raw_moves[member])
        ordered.append((qubit, scratch_column, to_column))

    return ordered


def column_precedence(ordered_moves, columns_of):
    """Predecessor sets so moves touching a shared column keep their sequence order.

    :param ordered_moves: moves in sequence order.
    :param columns_of: callable move -> iterable of columns it touches.
    :returns: list[set[int]] predecessor indices per move.
    """
    predecessors = [set() for _ in ordered_moves]
    last_by_column = {}
    for index, move in enumerate(ordered_moves):
        for column in columns_of(move):
            if column in last_by_column:
                predecessors[index].add(last_by_column[column])
            last_by_column[column] = index
    return predecessors


def left_edge(ordered_moves, key_of, conflicts, predecessors):
    """Left-edge greedy level packing under conflict and precedence constraints.

    :param ordered_moves: items to place.
    :param key_of: callable index -> sort key (left edge).
    :param conflicts: callable (index, other) -> bool, same-level conflict.
    :param predecessors: list[set[int]] from :func:`column_precedence`.
    :returns: list[int] level per item.
    """
    levels = [None] * len(ordered_moves)
    unplaced = set(range(len(ordered_moves)))
    level = 0
    while unplaced:
        placed_now = []
        for index in sorted(unplaced, key=key_of):
            if any(levels[predecessor] is None or levels[predecessor] >= level for predecessor in predecessors[index]):
                continue
            if any(conflicts(index, other) for other in placed_now):
                continue
            levels[index] = level
            placed_now.append(index)
        unplaced -= set(placed_now)
        level += 1
    return levels


def wire_only_floorplan(num_qubits, with_magic=False):
    """Build a degenerate Floorplan with no cells — straight wires only (pure-T circuits).

    :param num_qubits: int.
    :param with_magic: bool, reserve an extra magic-state column.
    :returns: Floorplan with zero layers.
    """
    magic_column = num_qubits + 1 if with_magic else None
    return Floorplan(num_qubits=num_qubits, box_widths=[], in_port_columns=[], out_port_columns=[], lane_columns=[], moves=[], gap_levels=[], magic_column=magic_column)


def _die_slot(floorplan, layer, qubit, use_out):
    columns = floorplan.out_port_columns[layer] if use_out else floorplan.in_port_columns[layer]
    if qubit in columns:
        row = next(row for row, _, box in floorplan.placements[layer] if qubit in box)
        return row, columns[qubit]
    return floorplan.parked_slots[layer][qubit]


def _in_gap_level(floorplan, boundary, column, slot_count, occupied, slot_levels, qubit_floor):
    if floorplan.die_dims is not None:
        reach = [floorplan.die_dims[0] - 1] * slot_count
        spacing = 1
    else:
        reach = [floorplan.magic_column + 2 * slot for slot in range(slot_count)]
        spacing = 2

    level = qubit_floor
    while True:
        for slot in range(slot_count):
            plane = 2 if slot == 0 else 0
            low, high = min(column, reach[slot]), max(column, reach[slot])
            if any(abs(level - used) < spacing for used in slot_levels.get((boundary, slot), ())):
                continue
            if any(used_plane == plane and not (high < used_low or used_high < low) for used_level, used_plane, used_low, used_high in occupied.get(boundary, ()) if used_level == level):
                continue
            return level, slot, (low, high, plane)
        level += 1


def place_injections(events, channels, floorplan, factory=None, factories=1):
    """Pin each injection event to a slot; pack it into the channel gap when possible.

    Interior-channel injections on unmoved qubits ride existing gap levels
    (level >= 0: crossbar packed with the jogs, per-slot spacing 2, gap_levels
    grown as needed) when the factory is cultivation-fast (interval_k <= 2).
    Everything else — head/tail channels, die mode, moved qubits, slow factories —
    keeps the block path (level == -1, grouped by round, <= INJECTION_BANDWIDTH).

    :param events: list[InjectionEvent].
    :param channels: list[int] from schedule_layers, parallel to events.
    :param floorplan: Floorplan.
    :param factory: FactorySpec | None.
    :param factories: int, factory units.
    :returns: list[InjectionPoint].
    """
    num_layers = len(floorplan.box_widths)
    bandwidth = INJECTION_BANDWIDTH
    fast_factory = factory is not None and factory.interval_k <= 2
    slot_count = min(INJECTION_BANDWIDTH, max(1, factories))
    points = []
    last_round = {}
    round_sizes = {}
    occupied = {}
    slot_levels = {}
    qubit_floor = {}

    if floorplan.die_dims is not None:
        for boundary in range(len(floorplan.grid_moves)):
            occupied[boundary] = [(gm.level, seg[2], seg[3], seg[4]) for gm in floorplan.grid_moves[boundary] for seg in gm.path if seg[0] == "I"]
    else:
        for boundary in range(len(floorplan.moves)):
            occupied[boundary] = [(move.level, move.plane, min(move.from_column, move.to_column), max(move.from_column, move.to_column)) for move in floorplan.moves[boundary]]

    for event, channel in zip(events, channels):
        row = 0
        if num_layers == 0:
            column = event.qubit + 1
        elif floorplan.die_dims is not None:
            layer = min(channel, num_layers - 1)
            row, column = _die_slot(floorplan, layer, event.qubit, channel >= num_layers)
        elif channel < num_layers:
            column = floorplan.in_port_columns[channel].get(event.qubit, floorplan.lane_columns[channel].get(event.qubit, event.qubit + 1))
        else:
            column = floorplan.out_port_columns[-1].get(event.qubit, floorplan.lane_columns[-1].get(event.qubit, event.qubit + 1))

        boundary = channel - 1
        if floorplan.die_dims is not None:
            movers = floorplan.grid_moves[boundary] if 0 <= boundary < len(floorplan.grid_moves) else ()
            in_gap = (fast_factory and floorplan.rows == 1 and 1 <= channel <= num_layers - 1
                      and not any(gm.qubit == event.qubit for gm in movers))
        else:
            in_gap = (fast_factory and 1 <= channel <= num_layers - 1
                      and not any(move.qubit == event.qubit for move in floorplan.moves[boundary]))

        if in_gap:
            floor = qubit_floor.get((boundary, event.qubit), 0)
            level, slot, span = _in_gap_level(floorplan, boundary, column, slot_count, occupied, slot_levels, floor)
            occupied[boundary].append((level, span[2], span[0], span[1]))
            slot_levels.setdefault((boundary, slot), []).append(level)
            qubit_floor[(boundary, event.qubit)] = level + 1
            floorplan.gap_levels[boundary] = max(floorplan.gap_levels[boundary], level + 1)
            points.append(InjectionPoint(qubit=event.qubit, dagger=event.dagger, channel=channel, column=column, row=row, slot=slot, level=level))
            continue

        start = last_round.get((channel, event.qubit), -1) + 1
        round_index = start
        while round_sizes.get((channel, round_index), 0) >= bandwidth:
            round_index += 1
        slot = round_sizes.get((channel, round_index), 0)
        round_sizes[(channel, round_index)] = slot + 1
        last_round[(channel, event.qubit)] = round_index
        points.append(InjectionPoint(qubit=event.qubit, dagger=event.dagger, channel=channel, column=column, row=row, round=round_index, slot=slot))

    return points


def _normalize_layers(member_sets):
    layers = []
    for layer_members in member_sets:
        if layer_members and isinstance(layer_members[0], int):
            layers.append([list(layer_members)])
        else:
            layers.append([list(box) for box in layer_members])
    return layers


def solve_floorplan(member_sets, num_qubits, side_ports=False, with_magic=False, die_dims=None):
    """Plan port columns, parking lanes and inter-layer qubit moves for all layers.

    :param member_sets: list per layer of a flat qubit list or list of per-box qubit lists.
    :param num_qubits: int.
    :param side_ports: bool, park/unpark through cell side faces (1-D only).
    :param with_magic: bool, reserve a magic-state column.
    :param die_dims: (width, rows | None) or None for 1-D.
    :returns: Floorplan.
    :raises ValueError: on empty/duplicate layers or die_dims overflow.
    """
    if not member_sets:
        raise ValueError("solve_floorplan: no layers")
    layers = _normalize_layers(member_sets)
    if die_dims is not None:
        from .solver2d import solve_2d

        if side_ports:
            raise ValueError("solve_floorplan: side_ports requires single-row (die_dims=None) mode")
        return solve_2d(layers, num_qubits, die_dims, with_magic)
    from .solver1d import solve_1d

    return solve_1d(layers, num_qubits, side_ports, with_magic)
