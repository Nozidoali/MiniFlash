from .floorplan import Floorplan, GridMove, column_precedence, left_edge, sequence_moves


def _segments(src, dst, corridor):
    (row_a, col_a), (row_b, col_b) = src, dst
    segs = []
    if row_a == row_b:
        if col_a != col_b:
            segs.append(("I", row_a, 2 if col_b > col_a else 0, min(col_a, col_b), max(col_a, col_b)))
        return tuple(segs)
    if corridor != col_a:
        segs.append(("I", row_a, 2 if corridor > col_a else 0, min(col_a, corridor), max(col_a, corridor)))
    segs.append(("J", corridor, min(row_a, row_b), max(row_a, row_b)))
    if corridor != col_b:
        segs.append(("I", row_b, 2 if col_b > corridor else 0, min(corridor, col_b), max(corridor, col_b)))
    return tuple(segs)


def _pick_corridor(src, dst, static, width):
    (row_a, _), (row_b, col_b) = src, dst
    if row_a == row_b:
        return col_b
    low, high = min(row_a, row_b), max(row_a, row_b)
    for delta in range(width):
        for corridor in (col_b + delta, col_b - delta):
            if 0 <= corridor < width and not any((row, corridor) in static for row in range(low, high + 1)):
                return corridor
    raise ValueError(f"solve_floorplan: no clear J corridor between rows {row_a} and {row_b} within die width {width}")


def _seg_conflict(seg_a, seg_b):
    if seg_a[0] == "I" and seg_b[0] == "I":
        return seg_a[1] == seg_b[1] and seg_a[2] == seg_b[2] and not (seg_a[4] < seg_b[3] or seg_b[4] < seg_a[3])
    if seg_a[0] == "J" and seg_b[0] == "J":
        return seg_a[1] == seg_b[1] and not (seg_a[3] < seg_b[2] or seg_b[3] < seg_a[2])
    i_seg, j_seg = (seg_a, seg_b) if seg_a[0] == "I" else (seg_b, seg_a)
    return i_seg[3] <= j_seg[1] <= i_seg[4] and j_seg[2] <= i_seg[1] <= j_seg[3]


def _pack_grid_levels(ordered, static, width):
    paths = [_segments(src, dst, _pick_corridor(src, dst, static, width)) for _, src, dst in ordered]

    def conflicts(index, other):
        return any(_seg_conflict(seg, other_seg) for seg in paths[index] for other_seg in paths[other])

    predecessors = column_precedence(ordered, lambda move: (move[1], move[2]))
    levels = left_edge(ordered, lambda index: min((seg[3] for seg in paths[index] if seg[0] == "I"), default=0), conflicts, predecessors)
    return [GridMove(qubit=qubit, src=src, dst=dst, level=level, path=path) for (qubit, src, dst), level, path in zip(ordered, levels, paths)]


def _corridor_relief(moves, static_of, immovable, width):
    for _, src, dst in moves:
        if src[0] == dst[0]:
            continue
        low, high = sorted((src[0], dst[0]))
        target = dst[1]
        best = None
        feasible = False
        for delta in range(width):
            for corridor in (target + delta, target - delta):
                if not 0 <= corridor < width:
                    continue
                blockers = [static_of[(row, corridor)] for row in range(low, high + 1) if (row, corridor) in static_of]
                if not blockers:
                    feasible = True
                    break
                if all(blocker not in immovable for blocker in blockers) and (best is None or len(blockers) < len(best)):
                    best = blockers
            if feasible:
                break
        if not feasible:
            return best
    return []


def solve_2d(layers, num_qubits, die_dims, with_magic):
    """Solve the 2-D (die-mode) floorplan: slot-stable placement plus 2-D grid moves.

    :param layers: list per layer of per-box qubit lists.
    :param num_qubits: int.
    :param die_dims: (width, rows | None), width is a hard cap.
    :param with_magic: bool, reserve the magic lane.
    :returns: Floorplan (die_dims/rows/grid_moves populated).
    :raises ValueError: when a box cannot fit the die.
    """
    width, row_cap = die_dims
    num_layers = len(layers)
    rows = row_cap if row_cap is not None else 1
    can_grow = row_cap is None

    position = {}
    placements, slots_by_layer, windows_by_layer, in_port_columns, out_port_columns, parked_slots = [], [], [], [], [], []

    def flat(qubit):
        row, col = position.get(qubit, (0, width + qubit))
        return row * width + col

    for layer, boxes in enumerate(layers):
        member_set = {qubit for box in boxes for qubit in box}
        parked = {qubit: position[qubit] for qubit in position if qubit not in member_set}
        ordered_boxes = sorted(boxes, key=lambda box: sum(flat(qubit) for qubit in box) / len(box)) if len(boxes) > 1 else list(boxes)

        placed = set()
        layout, pin_slots = [], {}
        for box in ordered_boxes:
            box_width = len(box) + 2
            seen = sorted((qubit for qubit in box if qubit in position), key=flat)
            unseen = sorted(qubit for qubit in box if qubit not in position)
            ordered = seen + unseen
            if seen:
                anchor = seen[len(seen) // 2]
                ideal_row = sorted(position[qubit][0] for qubit in seen)[len(seen) // 2]
                ideal_offset = max(0, position[anchor][1] - len(seen) // 2 - 1)
            else:
                ideal_row, ideal_offset = 0, 0

            best = None
            while best is None:
                for row in range(rows):
                    for offset in range(width - box_width + 1):
                        window = {(row, column) for column in range(offset, offset + box_width)}
                        if window & placed:
                            continue
                        evictions = sum(1 for slot in parked.values() if slot in window)
                        mismatches = sum(1 for rank, qubit in enumerate(seen) if (row, offset + 1 + rank) != position[qubit])
                        key = (mismatches + evictions, abs(row - ideal_row), abs(offset - ideal_offset), row, offset)
                        if best is None or key < best[0]:
                            best = (key, row, offset, window)
                if best is None:
                    if not can_grow:
                        raise ValueError(f"solve_floorplan: layer {layer} overflows die {die_dims}")
                    rows += 1

            _, row, offset, window = best
            placed |= window
            for rank, qubit in enumerate(ordered):
                pin_slots[qubit] = (row, offset + 1 + rank)
            layout.append((row, offset, list(box)))

        for qubit in [qubit for qubit, slot in parked.items() if slot in placed]:
            del parked[qubit]
        for qubit in sorted(qubit for qubit in range(num_qubits) if qubit not in member_set and qubit not in parked):
            near = position.get(qubit)
            while True:
                occupied = placed | set(parked.values())
                free = [(row, column) for row in range(rows) for column in range(width) if (row, column) not in occupied]
                if free:
                    parked[qubit] = min(free) if near is None else min(free, key=lambda slot: (abs(slot[0] - near[0]), abs(slot[1] - near[1]), slot))
                    break
                if not can_grow:
                    raise ValueError(f"solve_floorplan: layer {layer} overflows die {die_dims}")
                rows += 1

        placements.append(layout)
        slots_by_layer.append(dict(pin_slots))
        windows_by_layer.append(set(placed))
        in_port_columns.append({qubit: column for qubit, (_, column) in pin_slots.items()})
        out_port_columns.append({qubit: column for qubit, (_, column) in pin_slots.items()})
        parked_slots.append(dict(parked))
        position.update(pin_slots)
        position.update(parked)

    grid_moves, gap_levels = [], []
    for channel in range(num_layers - 1):
        for _ in range(2 * num_qubits + 2):
            before = dict(slots_by_layer[channel])
            before.update(parked_slots[channel])
            after = dict(slots_by_layer[channel + 1])
            after.update(parked_slots[channel + 1])
            raw = [(qubit, before[qubit], after[qubit]) for qubit in sorted(before) if qubit in after and before[qubit] != after[qubit]]
            occupied = set(before.values()) | set(after.values())
            free = [(row, column) for row in range(rows) for column in range(width) if (row, column) not in occupied]
            scratch_slot = min(free) if free else (rows, 0)

            ordered = sequence_moves(raw, scratch_slot)
            if len(ordered) > len(raw) and not free:
                if not can_grow:
                    raise ValueError(f"solve_floorplan: no scratch slot for move cycle in channel {channel} of die {die_dims}")
                rows += 1
                continue

            static_of = {before[qubit]: qubit for qubit in before if qubit in after and before[qubit] == after[qubit]}
            immovable = set(slots_by_layer[channel + 1])
            relocate = _corridor_relief(ordered, static_of, immovable, width)
            if relocate is None:
                raise ValueError(f"solve_floorplan: no clear J corridor in channel {channel} of die {die_dims}")
            if relocate:
                for qubit in relocate:
                    home = parked_slots[channel + 1][qubit]
                    while True:
                        taken = windows_by_layer[channel + 1] | set(parked_slots[channel + 1].values())
                        candidates = [(row, column) for row in range(rows) for column in range(width) if (row, column) not in taken]
                        if candidates:
                            parked_slots[channel + 1][qubit] = min(candidates, key=lambda slot: (slot[0] != home[0], slot))
                            break
                        if not can_grow:
                            raise ValueError(f"solve_floorplan: no relief slot in channel {channel} of die {die_dims}")
                        rows += 1
                continue

            packed = _pack_grid_levels(ordered, set(static_of), width)
            grid_moves.append(packed)
            gap_levels.append(max((move.level for move in packed), default=-1) + 1)
            break
        else:
            raise ValueError(f"solve_floorplan: corridor relief did not converge in channel {channel} of die {die_dims}")

    return Floorplan(num_qubits=num_qubits, box_widths=[width] * num_layers, in_port_columns=in_port_columns, out_port_columns=out_port_columns, lane_columns=[{} for _ in range(num_layers)], moves=[[] for _ in range(num_layers - 1)], gap_levels=gap_levels, side_exits=[{} for _ in range(num_layers)], side_entries=[{} for _ in range(num_layers)], die_dims=die_dims, rows=rows, placements=placements, parked_slots=parked_slots, grid_moves=grid_moves)
