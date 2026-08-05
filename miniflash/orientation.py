"""Cell orientation.

Public surface — three functions, matching the two hooks in
:func:`miniflash.synthesis.synthesize` plus the physics predicate:

- :func:`is_spacetime_symmetric` — a Y-free cell's spacetime diagram is
  symmetric under exchanging the space (I) and time (K) axes, so it can
  be re-sliced along either; the Y cube is the one genuinely
  time-directional object. Hadamard walls survive the exchange (a spatial
  domain wall is a single XZ merge/split, still a plain pipe).
- :func:`plan_chain_layers` — pre-synthesis: which layers sit in a
  structural chain and should be synthesized Y-free (``forbid_y``).
- :func:`apply_orientation` — post-synthesis: fuse chains of laid-down
  cells (singles are length-1 chains), rebuild the floorplan without the
  swallowed layers and remap injection channels.

The transform internals: ``_rotate_cell`` rotates 90 degrees in the I-K
plane (axes swap, uniform parity flip from the first-transverse-face
convention, hadamard walls end-aware on the reversed axis); ``_fuse_chain``
butts the rotated bodies along I — one connector pipe per qubit per seam,
a hadamard wall where the wire bit is 1, L-shaped elbows only at the chain
ends. In-pins reclaim their upright columns, so the floorplan's in-port
contract survives, and elbow corners flip the parity bit back: a fused
cell reports the first cell's in parities and the last cell's out
parities, leaving the wire ledger, in_bases threading and the synthesis
cache untouched. Validity is compose-then-rotate: the chain is
``rotate(A∘B∘…)``, the same topological diagram as the vertical stack.
"""
from dataclasses import replace

from .synthesis import AXIS_INDEX

_AXIS_SWAP = {"I": "K", "J": "J", "K": "I"}


def is_spacetime_symmetric(cell) -> bool:
    """True when the cell may be laid down in the I-K plane.

    :param cell: Cell.
    :returns: bool — False when any block is a ynode (Y cubes are
        time-directional: Y-basis init/measure at a temporal boundary).
    """
    return all(block["kind"] != "ynode" for block in cell.blocks)


def _rotated_position(position, dims, sense):
    i, j, k = position
    if sense > 0:
        return [k, j, dims[0] - 1 - i]
    return [dims[2] - 1 - k, j, i]


def _flip(parity):
    return parity if parity < 0 else 1 - parity


def _axis_reversed(axis, sense):
    return axis == ("I" if sense > 0 else "K")


def _rotate_cell(cell, sense=1):
    dims = cell.dims
    new_dims = [dims[2], dims[1], dims[0]]

    blocks = []
    for block in cell.blocks:
        if block["kind"] not in ("pipe", "hadamard"):
            raise ValueError(f"_rotate_cell: {block['kind']} block cannot rotate (see is_spacetime_symmetric)")
        parity = _flip(block["parity"])
        if block["kind"] == "hadamard" and _axis_reversed(block["axis"], sense):
            parity = _flip(parity)
        blocks.append({"kind": block["kind"], "pos": _rotated_position(block["pos"], dims, sense), "axis": _AXIS_SWAP[block["axis"]], "parity": parity})

    pins = []
    for pin in cell.pins:
        offset = _rotated_position(pin["offset"], dims, sense)
        direction = _AXIS_SWAP[pin["dir"]]
        end = "+" if offset[AXIS_INDEX[direction]] >= new_dims[AXIS_INDEX[direction]] - 1 else "-"
        pins.append({"name": pin["name"], "dir": direction, "end": end, "parity": _flip(pin["parity"]), "offset": offset})

    return replace(cell, dims=new_dims, pins=pins, blocks=blocks)


def _max_column(cell, prefix):
    return max((pin["offset"][0] // 2 for pin in cell.pins if pin["name"].startswith(prefix)), default=0)



def _chain_width(cells):
    return _max_column(cells[0], "in") + sum((cell.dims[2] + 1) // 2 for cell in cells) + _max_column(cells[-1], "out") + 2


def _fuse_chain(cells):
    if not cells:
        raise ValueError("_fuse_chain: empty chain")
    for cell in cells:
        if any(pin["dir"] != "K" for pin in cell.pins):
            raise ValueError("_fuse_chain: cell has non-K pins (side ports); cannot elbow")
    for previous, current in zip(cells, cells[1:]):
        if previous.dims[0] != current.dims[0] or previous.dims[1] != current.dims[1]:
            raise ValueError("_fuse_chain: mismatched cell cross-sections")
        out_columns = {pin["name"].rsplit("_", 1)[1]: pin["offset"][0] for pin in previous.pins if pin["name"].startswith("out")}
        in_columns = {pin["name"].rsplit("_", 1)[1]: pin["offset"][0] for pin in current.pins if pin["name"].startswith("in")}
        if out_columns != in_columns:
            raise ValueError(f"_fuse_chain: seam columns mismatch ({out_columns} vs {in_columns})")

    first = cells[0]
    max_in_column = _max_column(first, "in")
    width = _chain_width(cells)
    dims = [2 * width - 1, first.dims[1], first.dims[0]]
    top = dims[2] - 1
    body_start = 2 * (max_in_column + 1)

    blocks = []
    rotated_cells = [_rotate_cell(cell, sense=1) for cell in cells]
    shifts = []
    shift = body_start
    for rotated in rotated_cells:
        for block in rotated.blocks:
            position = list(block["pos"])
            position[0] += shift
            blocks.append({**block, "pos": position})
        shifts.append(shift)
        shift += rotated.dims[0] + 1

    for index, previous_cell in enumerate(cells[:-1]):
        seam_x = shifts[index] + rotated_cells[index].dims[0]
        for pin in previous_cell.pins:
            if not pin["name"].startswith("out"):
                continue
            level = top - pin["offset"][0]
            bit = max(0, pin["parity"])
            blocks.append({"kind": "hadamard" if bit else "pipe", "pos": [seam_x, pin["offset"][1], level], "axis": "I", "parity": 1 - bit})

    pins = []
    for pin in rotated_cells[0].pins:
        if not pin["name"].startswith("in"):
            continue
        level, plane_j, parity = pin["offset"][2], pin["offset"][1], pin["parity"]
        column = top - level
        for kk in range(1, level, 2):
            blocks.append({"kind": "pipe", "pos": [column, plane_j, kk], "axis": "K", "parity": _flip(parity)})
        for ii in range(column + 1, body_start, 2):
            blocks.append({"kind": "pipe", "pos": [ii, plane_j, level], "axis": "I", "parity": parity})
        pins.append({"name": pin["name"], "dir": "K", "end": "-", "parity": _flip(parity), "offset": [column, plane_j, -1]})

    out_base = shifts[-1] + rotated_cells[-1].dims[0] - 1
    for pin in rotated_cells[-1].pins:
        if not pin["name"].startswith("out"):
            continue
        level, plane_j, parity = pin["offset"][2], pin["offset"][1], pin["parity"]
        column = out_base + (top - level)
        for ii in range(out_base + 1, column, 2):
            blocks.append({"kind": "pipe", "pos": [ii, plane_j, level], "axis": "I", "parity": parity})
        for kk in range(level + 1, top, 2):
            blocks.append({"kind": "pipe", "pos": [column, plane_j, kk], "axis": "K", "parity": _flip(parity)})
        pins.append({"name": pin["name"], "dir": "K", "end": "+", "parity": _flip(parity), "offset": [column, plane_j, dims[2]]})

    seen_positions = set()
    for block in blocks:
        key = tuple(block["pos"])
        if key in seen_positions:
            raise RuntimeError(f"_fuse_chain: block collision at {key}")
        seen_positions.add(key)

    return replace(first, dims=dims, pins=pins, blocks=blocks, pauli_frame="|".join(cell.pauli_frame for cell in cells))



def _layer_structural(floorplan, layer):
    if len(floorplan.placements[layer]) != 1:
        return False
    if floorplan.lane_columns[layer]:
        return False
    if floorplan.side_exits and floorplan.side_exits[layer]:
        return False
    if floorplan.side_entries and floorplan.side_entries[layer]:
        return False
    return True


def _boundary_linked(floorplan, layer, blocked):
    following = layer + 1
    if following in blocked:
        return False
    if floorplan.in_port_columns[layer] != floorplan.out_port_columns[layer]:
        return False
    if floorplan.in_port_columns[following] != floorplan.out_port_columns[following]:
        return False
    if floorplan.out_port_columns[layer] != floorplan.in_port_columns[following]:
        return False
    return floorplan.placements[layer][0][1] == floorplan.placements[following][0][1]


def plan_chain_layers(floorplan, channels):
    """Pre-synthesis: layers sitting in a structural chain of length >= 2.

    These are worth synthesizing Y-free (``forbid_y``) so the chain can
    actually rotate — LaSsynth otherwise uses Y cubes freely as geometric
    terminators, which block rotation. Cell-level checks happen later in
    :func:`apply_orientation`; this sees only the floorplan and the
    injection channels.

    :param floorplan: Floorplan.
    :param channels: list per injection event of the layer it precedes.
    :returns: set of layer indices.
    """
    if floorplan.die_dims is not None:
        return set()
    num_layers = len(floorplan.box_widths)
    blocked = set(channels)
    chained = set()
    for layer in range(num_layers - 1):
        if _layer_structural(floorplan, layer) and _layer_structural(floorplan, layer + 1) and _boundary_linked(floorplan, layer, blocked):
            chained.add(layer)
            chained.add(layer + 1)
    return chained


def apply_orientation(floorplan, cell_types, channels):
    """Post-synthesis orientation pass: chains first, singles as length-1 chains.

    Groups maximal runs of consecutive layers that can lie down together
    (single box, rotatable K-pin cell, no parked lanes or side moves, and
    at every interior boundary: matching port columns and offsets, no
    injection). A group fuses when its lying K beats the upright K it
    replaces — ``Σ dims_k + interior gaps`` — and its width fits: under
    the magic column, or wider when no injection sits directly above the
    group's last layer (crossbars live in gap z-ranges and factory boxes
    only dip downward from their injection, so that is the one collision
    risk). For singletons the win reduces to the ``depth > n+2`` rule.
    Fused groups swallow their interior layers, so the floorplan is
    rebuilt (widths, port columns, placements, per-boundary moves — the
    boundary after a fused group is re-solved for the fresh out columns)
    and every injection channel is remapped to the surviving layer count.

    :param floorplan: 1-D Floorplan (returned unchanged in die mode).
    :param cell_types: list per layer of Cells.
    :param channels: list per injection event of the layer it precedes.
    :returns: (floorplan, cell_types, channels) — rebuilt copies.
    """
    from .floorplan import Floorplan
    from .solver1d import solve_channel

    num_layers = len(cell_types)
    if floorplan.die_dims is not None or num_layers == 0:
        return floorplan, cell_types, list(channels)
    width_cap = floorplan.magic_column
    blocked = set(channels)

    def eligible(layer):
        if len(cell_types[layer]) != 1 or not _layer_structural(floorplan, layer):
            return False
        cell = cell_types[layer][0]
        return is_spacetime_symmetric(cell) and all(pin["dir"] == "K" for pin in cell.pins)

    def linked(layer):
        return _boundary_linked(floorplan, layer, blocked)

    def fits(group):
        cells = [cell_types[layer][0] for layer in group]
        if width_cap is None:
            return True
        if floorplan.placements[group[0]][0][1] + _chain_width(cells) <= width_cap:
            return True
        return group[-1] + 1 not in blocked

    groups = []
    layer = 0
    while layer < num_layers:
        group = [layer]
        if eligible(layer):
            while group[-1] + 1 < num_layers and eligible(group[-1] + 1) and linked(group[-1]) and fits(group + [group[-1] + 1]):
                group.append(group[-1] + 1)
        groups.append(group)
        layer = group[-1] + 1

    def wins(group):
        if not eligible(group[0]) or not fits(group):
            return False
        cells = [cell_types[layer][0] for layer in group]
        upright = sum(cell.dims[2] for cell in cells)
        upright += sum(2 * (floorplan.gap_levels[boundary] + 1) for boundary in range(group[0], group[-1]))
        return cells[0].dims[0] < upright

    new_cells, widths, in_cols, out_cols, lanes, place, exits, entries = ([] for _ in range(8))
    boundary_info = []
    old_to_new = {}
    for group in groups:
        if wins(group):
            cells = [cell_types[layer][0] for layer in group]
            fused = _fuse_chain(cells)
            offset = floorplan.placements[group[0]][0][1]
            box_qubits = list(floorplan.placements[group[0]][0][2])
            out_local = {box_qubits[int(pin["name"].rsplit("_", 1)[1])]: pin["offset"][0] // 2 for pin in fused.pins if pin["name"].startswith("out")}
            old_to_new[group[0]] = len(new_cells)
            boundary_info.append((group[-1], True))
            new_cells.append([fused])
            widths.append((fused.dims[0] + 1) // 2)
            in_cols.append(dict(floorplan.in_port_columns[group[0]]))
            out_cols.append({qubit: offset + column for qubit, column in out_local.items()})
            lanes.append({})
            place.append([(0, offset, box_qubits)])
            exits.append({})
            entries.append({})
        else:
            for layer in group:
                old_to_new[layer] = len(new_cells)
                boundary_info.append((layer, False))
                new_cells.append(list(cell_types[layer]))
                widths.append(floorplan.box_widths[layer])
                in_cols.append(dict(floorplan.in_port_columns[layer]))
                out_cols.append(dict(floorplan.out_port_columns[layer]))
                lanes.append(dict(floorplan.lane_columns[layer]))
                place.append(list(floorplan.placements[layer]))
                exits.append(dict(floorplan.side_exits[layer]) if floorplan.side_exits else {})
                entries.append(dict(floorplan.side_entries[layer]) if floorplan.side_entries else {})

    moves, gap_levels = [], []
    for index in range(len(new_cells) - 1):
        boundary, fused_here = boundary_info[index]
        if fused_here:
            before = dict(out_cols[index])
            before.update(lanes[index])
            after = dict(in_cols[index + 1])
            after.update(lanes[index + 1])
            scratch = max([*before.values(), *after.values(), widths[index]]) + 1
            solved_moves, solved_gap = solve_channel(before, after, scratch)
            moves.append(solved_moves)
            gap_levels.append(solved_gap)
        else:
            moves.append(list(floorplan.moves[boundary]))
            gap_levels.append(floorplan.gap_levels[boundary])

    rebuilt = Floorplan(num_qubits=floorplan.num_qubits, box_widths=widths, in_port_columns=in_cols, out_port_columns=out_cols, lane_columns=lanes, moves=moves, gap_levels=gap_levels, side_exits=exits, side_entries=entries, magic_column=floorplan.magic_column, placements=place)
    remapped = [old_to_new[channel] if channel < num_layers else len(new_cells) for channel in channels]
    return rebuilt, new_cells, remapped
