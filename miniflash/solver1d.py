"""The 1-D floorplan solver.

Column-stable placement: every qubit keeps a home column for the whole
layout while cell boxes float around them. Between layers, wires the
next cell does not consume are swapped out to straight parking lanes and
swapped back on demand. Swap jogs are permutation-decomposed —
vacate-before-occupy chains, genuine cycles broken through a scratch
column — and packed into channel gap levels left-edge style. A pure
function; there is no search.
"""
import heapq

from .floorplan import Floorplan, INJECTION_BANDWIDTH, Move, SideMove, _column_precedence, _left_edge, _sequence_moves


def _parked_intervals(member_sets, num_qubits):
    num_layers = len(member_sets)
    members = [set(layer_members) for layer_members in member_sets]

    intervals = []
    for qubit in range(num_qubits):
        start = None
        for layer in range(num_layers):
            if qubit not in members[layer] and start is None:
                start = layer
            if qubit in members[layer] and start is not None:
                intervals.append((start, layer, qubit))
                start = None
        if start is not None:
            intervals.append((start, num_layers, qubit))

    return intervals


def _assign_lanes(intervals, strict=False):
    assignment = {}
    free_lanes = []
    active = []
    next_lane = 0

    for start, end, qubit in sorted(intervals):
        while active and (active[0][0] < start if strict else active[0][0] <= start):
            _, lane = heapq.heappop(active)
            heapq.heappush(free_lanes, lane)
        if free_lanes:
            lane = heapq.heappop(free_lanes)
        else:
            lane = next_lane
            next_lane += 1
        assignment[(qubit, start, end)] = lane
        heapq.heappush(active, (end, lane))

    return assignment, next_lane


def _intervals_by_qubit(lane_column_of):
    by_qubit = {}
    for (qubit, start, end), column in lane_column_of.items():
        by_qubit.setdefault(qubit, []).append((start, end, column))
    return by_qubit


def _assign_layer_columns(layers, num_qubits, lane_column_of):
    num_layers = len(layers)
    unions = [[qubit for box in boxes for qubit in box] for boxes in layers]
    by_qubit = _intervals_by_qubit(lane_column_of)

    def lane_column(qubit, layer):
        for start, end, column in by_qubit.get(qubit, []):
            if start <= layer < end:
                return column
        raise KeyError(f"qubit {qubit} not parked at layer {layer}")

    in_port_columns, out_port_columns, lane_columns, box_layouts = [], [], [], []
    position = {}
    for qubit in range(num_qubits):
        if qubit not in set(unions[0]):
            position[qubit] = lane_column(qubit, 0)

    for layer in range(num_layers):
        boxes = layers[layer]
        member_set = set(unions[layer])
        if len(boxes) > 1:
            boxes = sorted(boxes, key=lambda box: sum(position.get(qubit, qubit) for qubit in box) / len(box))
        next_members = set(unions[layer + 1]) if layer + 1 < num_layers else member_set

        in_columns, out_columns, layouts = {}, {}, []
        offset = 0
        for box in boxes:
            arrived = sorted(box, key=lambda qubit: position.get(qubit, qubit))
            for rank, qubit in enumerate(arrived):
                in_columns[qubit] = offset + rank + 1
            stayers = [qubit for qubit in arrived if qubit in next_members]
            swap_outs = [qubit for qubit in arrived if qubit not in next_members]
            swap_outs.sort(key=lambda qubit: lane_column(qubit, layer + 1) if layer + 1 < num_layers else 0)
            for rank, qubit in enumerate(stayers + swap_outs):
                out_columns[qubit] = offset + rank + 1
            layouts.append((0, offset, list(box)))
            offset += len(box) + 2

        in_port_columns.append(in_columns)
        out_port_columns.append(out_columns)
        lane_columns.append({qubit: lane_column(qubit, layer) for qubit in range(num_qubits) if qubit not in member_set})
        box_layouts.append(layouts)
        for qubit in range(num_qubits):
            if qubit in member_set:
                position[qubit] = out_columns[qubit]
            elif layer + 1 < num_layers and qubit not in next_members:
                position[qubit] = lane_column(qubit, layer + 1)
            elif layer + 1 < num_layers:
                position[qubit] = lane_column(qubit, layer)

    return in_port_columns, out_port_columns, lane_columns, box_layouts


def _pack_levels(ordered_moves):
    shapes = []
    for qubit, from_column, to_column in ordered_moves:
        plane = 2 if to_column > from_column else 0
        shapes.append((min(from_column, to_column), max(from_column, to_column), plane))

    def conflicts(index, other):
        low, high, plane = shapes[index]
        other_low, other_high, other_plane = shapes[other]
        return plane == other_plane and not (high < other_low or other_high < low)

    predecessors = _column_precedence(ordered_moves, lambda move: (move[1], move[2]))
    return _left_edge(ordered_moves, lambda index: shapes[index][0], conflicts, predecessors)


def _assign_side_slots(side_moves_by_layer, plane):
    slotted = []
    for layer_moves in side_moves_by_layer:
        ordered = sorted(layer_moves, key=lambda move: move[1])
        slotted.append({qubit: SideMove(qubit=qubit, lane_column=lane_column, slot=index, plane=plane) for index, (qubit, lane_column) in enumerate(ordered)})
    return slotted


def _stable_layer_columns(layers, num_qubits):
    num_layers = len(layers)
    position = {}
    ever_occupied = set()
    backfill = []
    in_port_columns, out_port_columns, lane_columns, box_layouts = [], [], [], []

    for layer in range(num_layers):
        members = {qubit for box in layers[layer] for qubit in box}
        parked = [qubit for qubit in range(num_qubits) if qubit not in members]
        parked_cols = {position[qubit] for qubit in parked if qubit in position}
        boxes = layers[layer]
        if len(boxes) > 1:
            boxes = sorted(boxes, key=lambda box: sum(position.get(qubit, num_qubits + qubit) for qubit in box) / len(box))

        layout, in_cols, out_cols = [], {}, {}
        placed = set()
        for box in boxes:
            seen = sorted((qubit for qubit in box if qubit in position), key=lambda qubit: position[qubit])
            unseen = sorted(qubit for qubit in box if qubit not in position)
            ordered = seen + unseen
            width = len(box) + 2
            ideal = max(0, position[seen[len(seen) // 2]] - len(seen) // 2 - 1) if seen else min(ever_occupied ^ ever_occupied | {0}) if not ever_occupied else max(ever_occupied) + 1
            blocked = parked_cols | placed

            best = None
            limit = max(ever_occupied, default=0) + width + num_qubits + 2
            for offset in range(0, limit):
                window = range(offset, offset + width)
                if any(column in blocked for column in window):
                    continue
                if any(offset + 1 + rank in ever_occupied for rank in range(len(seen), len(ordered))):
                    continue
                score = sum(1 for rank, qubit in enumerate(seen) if offset + 1 + rank != position[qubit])
                key = (score, abs(offset - ideal))
                if best is None or key < best[0]:
                    best = (key, offset)

            offset = best[1]
            placed.update(range(offset, offset + width))
            ever_occupied.update(range(offset, offset + width))
            for rank, qubit in enumerate(ordered):
                in_cols[qubit] = offset + rank + 1
                out_cols[qubit] = offset + rank + 1
            for qubit in unseen:
                backfill.append((qubit, in_cols[qubit], layer))
            layout.append((0, offset, list(box)))

        lane_columns.append({qubit: position[qubit] for qubit in parked if qubit in position})
        in_port_columns.append(in_cols)
        out_port_columns.append(out_cols)
        box_layouts.append(layout)
        for qubit in members:
            position[qubit] = out_cols[qubit]
            ever_occupied.add(out_cols[qubit])

    cursor = 0
    for qubit in range(num_qubits):
        if qubit not in position:
            while cursor in ever_occupied:
                cursor += 1
            backfill.append((qubit, cursor, num_layers))
            ever_occupied.add(cursor)
            cursor += 1

    for qubit, column, first_layer in backfill:
        for layer in range(first_layer):
            lane_columns[layer][qubit] = column
        for layer in range(first_layer, num_layers):
            if qubit not in in_port_columns[layer] and qubit not in lane_columns[layer]:
                lane_columns[layer][qubit] = column

    return in_port_columns, out_port_columns, lane_columns, box_layouts



def solve_1d(layers, num_qubits, side_ports, with_magic):
    """Solve the 1-D floorplan: port columns, lanes, channel moves, gap levels.

    :param layers: list per layer of per-box qubit lists.
    :param num_qubits: int.
    :param side_ports: bool, park/unpark through cell side faces.
    :param with_magic: bool, reserve the magic-state columns.
    :returns: Floorplan.
    :raises ValueError: on empty/duplicate layers or side_ports with multi-box layers.
    """
    for layer, boxes in enumerate(layers):
        union = [qubit for box in boxes for qubit in box]
        if not union:
            raise ValueError(f"solve_floorplan: layer {layer} has no members")
        if not set(union) <= set(range(num_qubits)):
            raise ValueError(f"solve_floorplan: layer {layer} members out of range")
        if len(set(union)) != len(union):
            raise ValueError(f"solve_floorplan: layer {layer} has duplicate members")
        if side_ports and len(boxes) > 1:
            raise ValueError("solve_floorplan: side_ports requires single-box layers")

    num_layers = len(layers)
    box_widths = [sum(len(box) + 2 for box in boxes) for boxes in layers]
    max_box_width = max(box_widths)
    member_unions = [[qubit for box in boxes for qubit in box] for boxes in layers]
    if side_ports:
        intervals = _parked_intervals(member_unions, num_qubits)
        assignment, num_lanes = _assign_lanes(intervals, strict=True)
        first_lane_column = max_box_width + 1
        lane_column_of = {key: first_lane_column + lane for key, lane in assignment.items()}
        in_port_columns, out_port_columns, lane_columns, box_layouts = _assign_layer_columns(layers, num_qubits, lane_column_of)
    else:
        num_lanes = 0
        in_port_columns, out_port_columns, lane_columns, box_layouts = _stable_layer_columns(layers, num_qubits)
        max_box_width = max((max((offset + len(box) + 2 for _, offset, box in layout), default=0) for layout in box_layouts), default=max_box_width)
        max_box_width = max(max_box_width, max((col + 1 for lanes in lane_columns for col in lanes.values()), default=0))
        first_lane_column = max_box_width

    exit_moves, entry_moves = [[] for _ in range(num_layers)], [[] for _ in range(num_layers)]
    if side_ports:
        for (qubit, start, end), column in lane_column_of.items():
            if start > 0:
                exit_moves[start - 1].append((qubit, column))
            if end < num_layers:
                entry_moves[end].append((qubit, column))
    side_exits = _assign_side_slots(exit_moves, 2)
    side_entries = _assign_side_slots(entry_moves, 0)
    for layer in range(num_layers):
        for qubit in side_exits[layer]:
            del out_port_columns[layer][qubit]
        for qubit in side_entries[layer]:
            del in_port_columns[layer][qubit]

    scratch_column = first_lane_column + num_lanes
    any_cycle = False
    moves, gap_levels = [], []
    for channel in range(num_layers - 1):
        before = dict(out_port_columns[channel])
        before.update(lane_columns[channel])
        after = dict(in_port_columns[channel + 1])
        after.update(lane_columns[channel + 1])
        raw_moves = [(qubit, before[qubit], after[qubit]) for qubit in sorted(before) if qubit in after and before[qubit] != after[qubit]]

        ordered = _sequence_moves(raw_moves, scratch_column)
        if len(ordered) > len(raw_moves):
            any_cycle = True
        levels = _pack_levels(ordered)
        moves.append([Move(qubit=qubit, from_column=from_column, to_column=to_column, level=level, plane=2 if to_column > from_column else 0) for level, (qubit, from_column, to_column) in zip(levels, ordered)])
        gap_levels.append(max(levels) + 1 if levels else 0)

    base_width = first_lane_column + num_lanes + (1 if any_cycle else 0)
    magic_column = None
    if with_magic:
        magic_column = base_width
        base_width += 2 * INJECTION_BANDWIDTH - 1

    return Floorplan(num_qubits=num_qubits, box_widths=box_widths, in_port_columns=in_port_columns, out_port_columns=out_port_columns, lane_columns=lane_columns, moves=moves, gap_levels=gap_levels, side_exits=side_exits, side_entries=side_entries, magic_column=magic_column, placements=box_layouts)
