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
:mod:`miniflash.placement1d` (column-stable 1-D) and
:mod:`miniflash.placement2d` (slot-stable die mode).
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Move:
    """One 1-D channel jog: a qubit relocating between columns on a track level."""

    #: qubit being moved
    qubit: int
    #: source column
    from_column: int
    #: destination column
    to_column: int
    #: track level inside the channel gap (0 = closest to the layer above)
    level: int
    #: J corridor of the horizontal run (0 or 2; the wire plane sits at 1)
    plane: int


@dataclass(frozen=True)
class SideMove:
    """A park/unpark move through a cell side face to a lane column."""

    #: qubit being parked or unparked
    qubit: int
    #: lane column the qubit waits in
    lane_column: int
    #: K slot of the side-face port (sets the port height on the cell wall)
    slot: int
    #: J corridor used between the cell face and the lane
    plane: int


@dataclass
class Floorplan:
    """The symbolic layout contract: every port column, lane, move and track,
    fixed before any cell is synthesized."""

    #: number of logical qubits
    num_qubits: int
    #: per layer, width of the cell band in columns
    box_widths: list
    #: per layer, {qubit: absolute column} of its in-port
    in_port_columns: list
    #: per layer, {qubit: absolute column} of its out-port
    out_port_columns: list
    #: per layer, {qubit: column} for qubits parked in a lane (1-D)
    lane_columns: list
    #: per channel, list[Move] jogs between consecutive layers (1-D)
    moves: list
    #: per channel, number of jog track levels in the gap
    gap_levels: list = field(default_factory=list)
    #: per layer, {qubit: SideMove} park moves out through a cell side face
    side_exits: list = field(default_factory=list)
    #: per layer, {qubit: SideMove} unpark moves back in through a side face
    side_entries: list = field(default_factory=list)
    #: leftmost column of the magic-state lane (1-D), None without injections
    magic_column: int = None
    #: (width, rows | None) die constraint, None for unconstrained 1-D
    die_dims: tuple = None
    #: number of die rows actually used
    rows: int = 0
    #: per layer, list of (row, column offset, box qubits) cell placements
    placements: list = field(default_factory=list)
    #: per layer, {qubit: (row, column)} parked slots (die mode)
    parked_slots: list = field(default_factory=list)
    #: per channel, list[GridMove] 2-D relocations (die mode)
    grid_moves: list = field(default_factory=list)


@dataclass(frozen=True)
class GridMove:
    """A die-mode 2-D relocation between (row, column) slots."""

    #: qubit being moved
    qubit: int
    #: (row, column) source slot
    src: tuple
    #: (row, column) destination slot
    dst: tuple
    #: track level inside the channel gap
    level: int
    #: route segments, e.g. ("J", corridor_column) for the row change
    path: tuple


INJECTION_BANDWIDTH = 2


@dataclass(frozen=True)
class InjectionPoint:
    """A placed T injection: which channel it fires in and where its crossbar sits."""

    #: data qubit receiving the T
    qubit: int
    #: True for ``tdg``
    dagger: bool
    #: channel (inter-layer gap) the injection fires in
    channel: int
    #: data-qubit column at that boundary
    column: int
    #: die row (die mode)
    row: int = 0
    #: production round within the channel
    round: int = 0
    #: crossbar slot within the round (0 | 1)
    slot: int = 0
    #: in-gap track level, or -1 for a block injection at the channel floor
    level: int = -1





def passthrough_floorplan(num_qubits):
    """Build a degenerate Floorplan with no cells — straight wires only (pure-T circuits).

    :param num_qubits: int.
    :returns: Floorplan with zero layers, magic column reserved.
    """
    return Floorplan(num_qubits=num_qubits, box_widths=[], in_port_columns=[], out_port_columns=[], lane_columns=[], moves=[], gap_levels=[], magic_column=num_qubits + 1)


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
    (level >= 0: crossbar packed with the jogs, gap_levels grown as needed)
    when the factory is cultivation-fast (interval_k <= 2). Everything else —
    head/tail channels, multi-row dies, moved qubits, slow factories — takes
    the block path (level == -1), grouped into per-(channel, row) rounds of
    the row's factory units; rounds larger than :data:`INJECTION_BANDWIDTH`
    are delivered through the backside bank.

    :param events: list[InjectionEvent].
    :param channels: list[int] from schedule_layers, parallel to events.
    :param floorplan: Floorplan.
    :param factory: FactorySpec | None.
    :param factories: int, factory units (machine total).
    :returns: list[InjectionPoint].
    """
    num_layers = len(floorplan.box_widths)
    paced = factory is not None and factory.interval_k > 0
    fast_factory = factory is not None and factory.interval_k <= 2
    supply = max(1, factories) if paced or factory is None else max(INJECTION_BANDWIDTH, factories)
    if paced and floorplan.die_dims is not None and floorplan.rows > 1 and factories < floorplan.rows:
        raise ValueError("place_injections: backside supply needs at least one factory unit per die row")
    row_supply = {}
    if paced and floorplan.die_dims is not None and floorplan.rows > 1 and factories >= floorplan.rows:
        per_row, extra = divmod(factories, floorplan.rows)
        row_supply = {r: per_row + (1 if r < extra else 0) for r in range(floorplan.rows)}
    slot_count = min(INJECTION_BANDWIDTH, max(1, factories))
    points = []
    last_round = {}
    round_sizes = {}
    round_totals = {}
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
            in_gap = fast_factory and floorplan.rows == 1 and 1 <= channel <= num_layers - 1
        else:
            movers = floorplan.moves[boundary] if 0 <= boundary < len(floorplan.moves) else ()
            in_gap = fast_factory and floorplan.magic_column is not None and 1 <= channel <= num_layers - 1

        if in_gap:
            own_jogs = [move.level for move in movers if move.qubit == event.qubit]
            floor = max(qubit_floor.get((boundary, event.qubit), 0), max(own_jogs) + 1 if own_jogs else 0)
            level, slot, span = _in_gap_level(floorplan, boundary, column, slot_count, occupied, slot_levels, floor)
            occupied[boundary].append((level, span[2], span[0], span[1]))
            slot_levels.setdefault((boundary, slot), []).append(level)
            qubit_floor[(boundary, event.qubit)] = level + 1
            floorplan.gap_levels[boundary] = max(floorplan.gap_levels[boundary], level + 1)
            points.append(InjectionPoint(qubit=event.qubit, dagger=event.dagger, channel=channel, column=column, row=row, slot=slot, level=level))
            continue

        row_cap = row_supply.get(row, supply) if paced else supply
        start = last_round.get((channel, event.qubit), -1) + 1
        round_index = start
        while (round_sizes.get((channel, row, round_index), 0) >= row_cap
               or (not row_supply and paced and round_totals.get((channel, round_index), 0) >= supply)):
            round_index += 1
        slot = round_sizes.get((channel, row, round_index), 0)
        round_sizes[(channel, row, round_index)] = slot + 1
        round_totals[(channel, round_index)] = round_totals.get((channel, round_index), 0) + 1
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
        from .placement2d import solve_2d

        if side_ports:
            raise ValueError("solve_floorplan: side_ports requires single-row (die_dims=None) mode")
        return solve_2d(layers, num_qubits, die_dims, with_magic)
    from .placement1d import solve_1d

    return solve_1d(layers, num_qubits, side_ports, with_magic)
