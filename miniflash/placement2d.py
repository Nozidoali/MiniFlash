"""The 2-D floorplan solver (die mode).

Slot-stable placement under a hard die width: qubits hold (row, column)
homes, cell boxes are admitted row by row, and congestion is relieved by
eviction and corridor moves. Channel moves generalize to 2-D L/Z grid
paths, solved per channel by :func:`miniflash.channel.solve`; corridor
relief (evicting parked qubits) stays here as the caller's policy over
the pure packer.
"""
from .channel import ChannelInfeasible, Lane, RearrangementChannel, solve
from .floorplan import Floorplan


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
            occupied = set(before.values()) | set(after.values())
            free = [(row, column) for row in range(rows) for column in range(width) if (row, column) not in occupied]
            static_of = {before[qubit]: qubit for qubit in before if qubit in after and before[qubit] == after[qubit]}
            immovable = set(slots_by_layer[channel + 1])

            lanes = {qubit: Lane(start=before[qubit], end=after[qubit]) for qubit in sorted(before) if qubit in after}
            problem = RearrangementChannel(lanes=lanes, static=frozenset(static_of), scratch=tuple(free[:1]), width=width)
            try:
                plan = solve(problem)
            except ChannelInfeasible as infeasible:
                if infeasible.scratch:
                    if not can_grow:
                        raise ValueError(f"solve_floorplan: no scratch slot for move cycle in channel {channel} of die {die_dims}")
                    rows += 1
                    continue
                movable = [blockers for blockers in infeasible.blocker_sets if blockers and all(static_of[slot] not in immovable for slot in blockers)]
                if not movable:
                    raise ValueError(f"solve_floorplan: no clear J corridor in channel {channel} of die {die_dims}")
                for slot in min(movable, key=len):
                    qubit = static_of[slot]
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

            grid_moves.append(list(plan.moves))
            gap_levels.append(plan.levels)
            break
        else:
            raise ValueError(f"solve_floorplan: corridor relief did not converge in channel {channel} of die {die_dims}")

    return Floorplan(num_qubits=num_qubits, box_widths=[width] * num_layers, in_port_columns=in_port_columns, out_port_columns=out_port_columns, lane_columns=[{} for _ in range(num_layers)], moves=[[] for _ in range(num_layers - 1)], gap_levels=gap_levels, side_exits=[{} for _ in range(num_layers)], side_entries=[{} for _ in range(num_layers)], die_dims=die_dims, rows=rows, placements=placements, parked_slots=parked_slots, grid_moves=grid_moves)
