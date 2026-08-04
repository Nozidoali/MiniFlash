import base64
import json
import struct

from .floorplan import INJECTION_BANDWIDTH
from .synthesis import AXIS_INDEX, STRIDE

WIRE_J = STRIDE


def _make_pipe(low, high, axis, parity):
    return {"lo": list(low), "hi": list(high), "axis": axis, "parity": int(parity)}


def emit_straight_run(pipes, column, plane_j, z_start, z_end, parity_bit):
    """Emit a vertical wire run at column from z_start to z_end.

    :returns: None; appends to ``pipes``.
    """
    for z in range(z_start, z_end, STRIDE):
        pipes.append(_make_pipe([column, plane_j, z], [column, plane_j, z + STRIDE], "K", parity_bit))


def emit_flip_landing(pipes, column, z_start, z_bottom, parity_bit):
    """Emit a wire landing that absorbs a pending Hadamard (color flip) before z_bottom.

    :returns: None; appends to ``pipes``.
    """
    emit_straight_run(pipes, column, WIRE_J, z_start, z_bottom - STRIDE, parity_bit)
    hadamard = _make_pipe([column, WIRE_J, z_bottom - STRIDE], [column, WIRE_J, z_bottom], "K", parity_bit)
    hadamard["hadamard"] = True
    pipes.append(hadamard)


def emit_move_chain(pipes, chain, z_top, z_bottom, parity_bit, flip_at_end=False):
    """Emit one qubit's channel jogs: straight run, J-hop, I-run, J-hop per move.

    :param chain: list[Move] for one qubit in one channel, level order.
    :param flip_at_end: bool, land through :func:`emit_flip_landing`.
    :returns: None; appends to ``pipes``.
    """
    column = chain[0].from_column * STRIDE
    z_previous = z_top
    hop_bit = 1 - parity_bit
    for move in chain:
        jog_z = z_top + (move.level + 1) * STRIDE
        emit_straight_run(pipes, column, WIRE_J, z_previous, jog_z, parity_bit)
        plane_j = move.plane * STRIDE
        pipes.append(_make_pipe([column, min(WIRE_J, plane_j), jog_z], [column, max(WIRE_J, plane_j), jog_z], "J", hop_bit))
        next_column = move.to_column * STRIDE
        step = STRIDE if next_column > column else -STRIDE
        for i in range(column, next_column, step):
            low, high = sorted((i, i + step))
            pipes.append(_make_pipe([low, plane_j, jog_z], [high, plane_j, jog_z], "I", parity_bit))
        pipes.append(_make_pipe([next_column, min(WIRE_J, plane_j), jog_z], [next_column, max(WIRE_J, plane_j), jog_z], "J", hop_bit))
        column, z_previous = next_column, jog_z

    if flip_at_end:
        emit_flip_landing(pipes, column, z_previous, z_bottom, parity_bit)
    else:
        emit_straight_run(pipes, column, WIRE_J, z_previous, z_bottom, parity_bit)


def _make_side_pipe(low, high, axis, parity):
    pipe = _make_pipe(low, high, axis, parity)
    pipe["side"] = True
    return pipe


def _emit_side_face_run(pipes, face_cube, hop_cube, z_port, parity_bit, flip):
    for i in range(face_cube, hop_cube, STRIDE):
        pipe = _make_side_pipe([i, WIRE_J, z_port], [i + STRIDE, WIRE_J, z_port], "I", 0 if flip and i == face_cube else parity_bit)
        if flip and i == face_cube:
            pipe["hadamard"] = True
        pipes.append(pipe)


def emit_side_exit(pipes, face_cube, hop_cube, lane_cube, plane_j, z_port, z_bottom, parity_bit):
    """Emit a park move through a cell side face out to its lane column.

    :returns: None; appends to ``pipes``.
    """
    _emit_side_face_run(pipes, face_cube, hop_cube, z_port, parity_bit, False)
    hop_bit = 1 - parity_bit
    pipes.append(_make_side_pipe([hop_cube, min(WIRE_J, plane_j), z_port], [hop_cube, max(WIRE_J, plane_j), z_port], "J", hop_bit))
    for i in range(hop_cube, lane_cube, STRIDE):
        pipes.append(_make_side_pipe([i, plane_j, z_port], [i + STRIDE, plane_j, z_port], "I", parity_bit))
    pipes.append(_make_side_pipe([lane_cube, min(WIRE_J, plane_j), z_port], [lane_cube, max(WIRE_J, plane_j), z_port], "J", hop_bit))
    for z in range(z_port + 1, z_bottom, STRIDE):
        pipes.append(_make_side_pipe([lane_cube, WIRE_J, z], [lane_cube, WIRE_J, z + STRIDE], "K", parity_bit))


def emit_side_entry(pipes, face_cube, hop_cube, lane_cube, plane_j, z_start, z_port, parity_bit, flip):
    """Emit an unpark move from a lane column into a cell side face.

    :returns: None; appends to ``pipes``.
    """
    hop_bit = 1 - parity_bit
    for z in range(z_start, z_port, STRIDE):
        pipes.append(_make_side_pipe([lane_cube, WIRE_J, z], [lane_cube, WIRE_J, z + STRIDE], "K", parity_bit))
    pipes.append(_make_side_pipe([lane_cube, min(WIRE_J, plane_j), z_port], [lane_cube, max(WIRE_J, plane_j), z_port], "J", hop_bit))
    for i in range(hop_cube, lane_cube, STRIDE):
        pipes.append(_make_side_pipe([i, plane_j, z_port], [i + STRIDE, plane_j, z_port], "I", parity_bit))
    pipes.append(_make_side_pipe([hop_cube, min(WIRE_J, plane_j), z_port], [hop_cube, max(WIRE_J, plane_j), z_port], "J", hop_bit))
    _emit_side_face_run(pipes, face_cube, hop_cube, z_port, parity_bit, flip)


def emit_cell_pipes(pipes, cell, z_offset, x_offset=0, y_offset=0):
    """Emit a synthesized cell's blocks translated to its die offsets.

    :returns: None; appends to ``pipes``.
    """
    for block in cell.blocks:
        if block["kind"] not in ("pipe", "hadamard"):
            continue
        position = [block["pos"][0] + x_offset, block["pos"][1] + y_offset, block["pos"][2] + z_offset]
        low, high = list(position), list(position)
        axis_index = AXIS_INDEX[block["axis"]]
        low[axis_index] -= 1
        high[axis_index] += 1
        pipe = _make_pipe(low, high, block["axis"], block.get("parity", 0))
        if block["kind"] == "hadamard":
            pipe["hadamard"] = True
        pipes.append(pipe)


INJECTION_BLOCK_LEVELS = 2
ROW_PITCH = 6


def _emit_j_run(pipes, x, y_start, y_end, z, parity_bit):
    for y in range(min(y_start, y_end), max(y_start, y_end), STRIDE):
        pipes.append(_make_pipe([x, y, z], [x, y + STRIDE, z], "J", parity_bit))


def wire_y(row):
    """J coordinate of a row's wire plane.

    :param row: int die row.
    :returns: int J coordinate.
    """
    return row * ROW_PITCH + WIRE_J


def _grid_waypoints(move):
    (row_a, col_a), (row_b, col_b) = move.src, move.dst
    corridor = next((seg[1] for seg in move.path if seg[0] == "J"), col_b)
    points = [(col_a, wire_y(row_a))]
    if row_a == row_b:
        plane = row_a * ROW_PITCH + (2 * STRIDE if col_b > col_a else 0)
        points += [(col_a, plane), (col_b, plane)]
    else:
        if corridor != col_a:
            plane = row_a * ROW_PITCH + (2 * STRIDE if corridor > col_a else 0)
            points += [(col_a, plane), (corridor, plane)]
        if corridor != col_b:
            plane = row_b * ROW_PITCH + (2 * STRIDE if col_b > corridor else 0)
            points += [(corridor, plane), (col_b, plane)]
    points.append((col_b, wire_y(row_b)))
    return [point for index, point in enumerate(points) if index == 0 or point != points[index - 1]]


def emit_grid_path(pipes, move, jog_z, parity_bit):
    """Emit a die-mode 2-D grid move (I/J segments at jog_z).

    :returns: None; appends to ``pipes``.
    """
    hop_bit = 1 - parity_bit
    points = _grid_waypoints(move)
    for (col_a, y_a), (col_b, y_b) in zip(points, points[1:]):
        if y_a == y_b:
            step = STRIDE if col_b > col_a else -STRIDE
            for x in range(col_a * STRIDE, col_b * STRIDE, step):
                low, high = sorted((x, x + step))
                pipes.append(_make_pipe([low, y_a, jog_z], [high, y_a, jog_z], "I", parity_bit))
        else:
            _emit_j_run(pipes, col_a * STRIDE, y_a, y_b, jog_z, hop_bit)


def emit_die_injection(pipes, column, row, width, z0, parity_bit, slot=0):
    """Emit a die-mode injection crossbar at z0 spanning the row width.

    :returns: None; appends to ``pipes``.
    """
    z_zz = z0 + STRIDE
    data_cube = column * STRIDE
    plane = row * ROW_PITCH + (2 * STRIDE if slot == 0 else 0)
    hop_bit = 1 - parity_bit
    for y in range(min(wire_y(row), plane), max(wire_y(row), plane), STRIDE):
        pipe = _make_pipe([data_cube, y, z_zz], [data_cube, y + STRIDE, z_zz], "J", hop_bit)
        pipe["t_volume"] = True
        pipes.append(pipe)
    for x in range(data_cube, width * STRIDE, STRIDE):
        pipe = _make_pipe([x, plane, z_zz], [x + STRIDE, plane, z_zz], "I", parity_bit)
        pipe["t_volume"] = True
        pipes.append(pipe)


def _make_t_pipe(low, high, axis, parity):
    pipe = _make_pipe(low, high, axis, parity)
    pipe["t_volume"] = True
    return pipe


def emit_injection(pipes, column, magic_column, z0, parity_bit, slot=0):
    """Emit a 1-D injection crossbar from column to the magic lane at z0.

    :returns: None; appends to ``pipes``.
    """
    data_cube = column * STRIDE
    magic_cube = (magic_column + 2 * slot) * STRIDE
    z_zz = z0 + STRIDE
    plane_j = 2 * STRIDE if slot == 0 else 0
    hop_bit = 1 - parity_bit
    pipes.append(_make_t_pipe([magic_cube, WIRE_J, z0], [magic_cube + STRIDE, WIRE_J, z0], "I", 0))
    pipes.append(_make_t_pipe([magic_cube, WIRE_J, z0], [magic_cube, WIRE_J, z_zz], "K", 0))
    pipes.append(_make_t_pipe([data_cube, min(WIRE_J, plane_j), z_zz], [data_cube, max(WIRE_J, plane_j), z_zz], "J", hop_bit))
    for i in range(data_cube, magic_cube, STRIDE):
        pipes.append(_make_t_pipe([i, plane_j, z_zz], [i + STRIDE, plane_j, z_zz], "I", parity_bit))
    pipes.append(_make_t_pipe([magic_cube, min(WIRE_J, plane_j), z_zz], [magic_cube, max(WIRE_J, plane_j), z_zz], "J", hop_bit))


def check_program(program):
    """Check Program IR consistency before lowering.

    Net endpoints must match cell pins, move levels must fit channel tracks,
    1-D jogs must not overlap on a (level, plane) corridor, die-mode macros
    must fit the die width.

    :param program: Program.
    :returns: list[str] error messages (empty = valid).
    """
    errors = []
    out_cols = {}
    in_cols = {}
    for index, macro in enumerate(program.macros):
        if macro.kind != "cell":
            continue
        for pin in macro.ref.pins:
            slot = ("pin", macro.row, macro.offset + pin["offset"][0] // 2)
            target = out_cols if pin["name"].startswith("out") else in_cols
            target.setdefault(macro.layer, set()).add(slot)

    for net in program.nets:
        if net.source[0] == "pin" and net.source[1:] != () and tuple(net.source) not in {("pin",) + s[1:] for s in out_cols.get(net.channel, set())}:
            errors.append(f"net q{net.qubit}@{net.channel}: source {net.source} matches no out pin")
        if net.sink[0] == "pin" and tuple(net.sink) not in {("pin",) + s[1:] for s in in_cols.get(net.channel + 1, set())}:
            errors.append(f"net q{net.qubit}@{net.channel}: sink {net.sink} matches no in pin")

    for channel in program.channels:
        moves = [net for net in program.nets if net.channel == channel.index and net.track >= 0]
        for net in moves:
            for level, *_ in net.path:
                if level >= channel.tracks:
                    errors.append(f"net q{net.qubit}@{channel.index}: track {level} >= channel tracks {channel.tracks}")
        if program.die_dims is None:
            per_level = {}
            for net in moves:
                for level, from_col, to_col, plane in net.path:
                    low, high = min(from_col, to_col), max(from_col, to_col)
                    for other_low, other_high, other_qubit in per_level.get((level, plane), []):
                        if not (high < other_low or other_high < low):
                            errors.append(f"channel {channel.index} level {level} plane {plane}: q{net.qubit} overlaps q{other_qubit}")
                    per_level.setdefault((level, plane), []).append((low, high, net.qubit))

    if program.die_dims is not None:
        width = program.die_dims[0]
        for macro in program.macros:
            if macro.kind == "cell" and macro.offset + len([p for p in macro.ref.pins if p["name"].startswith("in")]) + 2 > width:
                errors.append(f"macro layer {macro.layer} offset {macro.offset} exceeds die width {width}")

    num_layers = len(program.box_widths)
    for macro in program.macros:
        if macro.kind == "injection" and macro.level >= 0:
            if not 1 <= macro.layer <= num_layers - 1:
                errors.append(f"in-gap injection at channel {macro.layer}: not an interior channel")
            elif macro.level >= program.channels[macro.layer - 1].tracks:
                errors.append(f"in-gap injection at channel {macro.layer}: level {macro.level} >= tracks {program.channels[macro.layer - 1].tracks}")

    blocks = {}
    for macro in program.macros:
        if macro.kind == "injection" and macro.level < 0:
            blocks.setdefault((macro.layer, macro.round), []).append(macro)
    for (channel, round_index), members in blocks.items():
        if len(members) > 2:
            errors.append(f"injection block channel {channel} round {round_index}: {len(members)} members > bandwidth 2")
        if len({member.qubits[0] for member in members}) != len(members):
            errors.append(f"injection block channel {channel} round {round_index}: duplicate qubit")
        if len({member.slot for member in members}) != len(members):
            errors.append(f"injection block channel {channel} round {round_index}: duplicate slot")

    return errors


def build_layout(program):
    """Check a Program and lower it to concrete 3-D geometry (pipes) and volume stats.

    :param program: Program.
    :returns: layout dict — pipes (list of {lo, hi, axis, parity, ...}), volume,
        occupied_volume, cube_envelope, bbox, route_stats; plus compute_volume,
        factory_boxes, t_count and wait_levels when the circuit has injections.
    :raises RuntimeError: when the IR check fails.
    """
    problems = check_program(program)
    if problems:
        raise RuntimeError(f"build_layout: IR check failed: {problems[:3]}")

    from .floorplan import GridMove, Move

    num_layers = len(program.box_widths)
    die_dims = program.die_dims
    layers = [[] for _ in range(num_layers)]
    for macro in program.macros:
        if macro.kind == "cell":
            layers[macro.layer].append(macro)
    factory = next((macro.ref for macro in program.macros if macro.kind == "factory"), None)
    factories = max(1, sum(1 for macro in program.macros if macro.kind == "factory"))
    injections = [macro for macro in program.macros if macro.kind == "injection"]
    colors = {}
    for net in program.nets:
        colors.setdefault(net.channel, {})[net.qubit] = net.color
    layer_depths = [max(macro.ref.dims[2] for macro in layer) for layer in layers]
    gap_heights = [(channel.tracks + 1) * STRIDE for channel in program.channels]

    def bits_at(channel):
        return program.exit_colors if channel >= num_layers - 1 else colors.get(channel, {})

    def cols(layer, prefix):
        slots = {}
        for macro in layers[layer]:
            for pin in macro.ref.pins:
                if pin["name"].startswith(prefix) and pin["dir"] == "K":
                    slots[macro.qubits[int(pin["name"].rsplit("_", 1)[1])]] = (macro.row, macro.offset + pin["offset"][0] // 2)
        return slots

    blocks_by_channel = {}
    in_gap_by_boundary = {}
    for index, macro in enumerate(injections):
        if macro.level >= 0:
            in_gap_by_boundary.setdefault(macro.layer - 1, []).append(index)
        else:
            blocks_by_channel.setdefault(macro.layer, {}).setdefault(macro.round, []).append(index)

    injection_z0 = [None] * len(injections)
    injection_factory = [0] * len(injections)
    last_zz = [0] * factories
    wait_levels = 0

    def _place_blocks(channel, z):
        nonlocal wait_levels
        for round_index in sorted(blocks_by_channel.get(channel, {})):
            members = blocks_by_channel[channel][round_index]
            candidate_zz = z + STRIDE
            if factory is not None:
                span = factory.interval_k * STRIDE
                ready = [value + span for value in last_zz]
                chosen = []
                shortfall = 0
                for _ in members:
                    unit = min(range(factories), key=lambda machine: ready[machine])
                    shortfall = max(shortfall, ready[unit] - candidate_zz)
                    ready[unit] += span
                    chosen.append(unit)

                if shortfall > 0:
                    wait = -(-shortfall // STRIDE) * STRIDE
                    z += wait
                    wait_levels += wait // STRIDE
                    candidate_zz = z + STRIDE

                counts = {}
                for member_index, unit in zip(members, chosen):
                    injection_factory[member_index] = unit
                    counts[unit] = counts.get(unit, 0) + 1
                for unit, count in counts.items():
                    last_zz[unit] = candidate_zz + span * (count - 1)

            for member_index in members:
                injection_z0[member_index] = z
            z += INJECTION_BLOCK_LEVELS * STRIDE

        return z

    z_offsets, z = [], 0
    z = _place_blocks(0, z)
    for layer in range(num_layers):
        z_offsets.append(z)
        z += layer_depths[layer]
        if layer < num_layers - 1:
            z += gap_heights[layer]
            z = _place_blocks(layer + 1, z)

    if num_layers > 0:
        z = _place_blocks(num_layers, z)
    z_end = z

    pipes = []
    for layer in range(num_layers):
        out_slots = cols(layer, "out")
        for macro in layers[layer]:
            emit_cell_pipes(pipes, macro.ref, z_offsets[layer], macro.offset * STRIDE, macro.row * ROW_PITCH)
        for macro in layers[layer]:
            depth = macro.ref.dims[2]
            if depth < layer_depths[layer]:
                for qubit in macro.qubits:
                    row, col = out_slots[qubit]
                    emit_straight_run(pipes, col * STRIDE, wire_y(row), z_offsets[layer] + depth, z_offsets[layer] + layer_depths[layer], bits_at(layer).get(qubit, 0))
        for qubit, (row, col) in program.parking[layer].items():
            parity = colors.get(layer - 1, {}).get(qubit, 0) if layer > 0 else 0
            emit_straight_run(pipes, col * STRIDE, wire_y(row), z_offsets[layer], z_offsets[layer] + layer_depths[layer], parity)

    for channel in range(num_layers - 1):
        z_top = z_offsets[channel] + layer_depths[channel]
        z_bottom = z_offsets[channel + 1]
        bits = bits_at(channel)

        for net in program.nets:
            if net.channel != channel:
                continue
            parity = bits.get(net.qubit, 0)
            if die_dims is not None:
                z_previous = z_top
                slot = net.source[1:]
                for level, src, dst, *segments in net.path:
                    jog_z = z_top + (level + 1) * STRIDE
                    emit_straight_run(pipes, slot[1] * STRIDE, wire_y(slot[0]), z_previous, jog_z, parity)
                    emit_grid_path(pipes, GridMove(qubit=net.qubit, src=tuple(src), dst=tuple(dst), level=level, path=tuple(segments)), jog_z, parity)
                    slot, z_previous = tuple(dst), jog_z
                if net.flip:
                    emit_flip_landing(pipes, slot[1] * STRIDE, z_previous, z_bottom, parity)
                else:
                    emit_straight_run(pipes, slot[1] * STRIDE, wire_y(slot[0]), z_previous, z_bottom, parity)
            elif net.path:
                chain = [Move(qubit=net.qubit, from_column=from_column, to_column=to_column, level=level, plane=plane) for level, from_column, to_column, plane in net.path]
                emit_move_chain(pipes, chain, z_top, z_bottom, parity, flip_at_end=net.flip)
            else:
                column = net.source[2]
                if net.flip:
                    emit_flip_landing(pipes, column * STRIDE, z_top, z_bottom, parity)
                else:
                    emit_straight_run(pipes, column * STRIDE, WIRE_J, z_top, z_bottom, parity)

        for index in in_gap_by_boundary.get(channel, ()):
            macro = injections[index]
            jog_z = z_top + (macro.level + 1) * STRIDE
            injection_z0[index] = jog_z - STRIDE
            if die_dims is not None:
                emit_die_injection(pipes, macro.offset, macro.row, die_dims[0], jog_z - STRIDE, bits.get(macro.qubits[0], 0), slot=macro.slot)
            else:
                emit_injection(pipes, macro.offset, program.magic_column, jog_z - STRIDE, bits.get(macro.qubits[0], 0), slot=macro.slot)

        if die_dims is None and program.sides:
            exit_face_cube = (program.box_widths[channel] - 1) * STRIDE
            hop_cube = max(program.box_widths) * STRIDE
            for qubit, (lane_column, slot, plane) in program.sides[channel][0].items():
                z_port = z_offsets[channel] + layer_depths[channel] - 1 - 2 * slot
                emit_side_exit(pipes, exit_face_cube, hop_cube, lane_column * STRIDE, plane * STRIDE, z_port, z_bottom, 0)
            entry_face_cube = (program.box_widths[channel + 1] - 1) * STRIDE
            for qubit, (lane_column, slot, plane) in program.sides[channel + 1][1].items():
                z_port = z_bottom + 2 * slot
                emit_side_entry(pipes, entry_face_cube, hop_cube, lane_column * STRIDE, plane * STRIDE, z_bottom, z_port, bits.get(qubit, 0), bits.get(qubit, 0) == 1)

    if injections:
        if num_layers > 0 and z_offsets[0] > 0:
            head = dict(cols(0, "in"))
            head.update(program.parking[0])
            for qubit, (row, col) in head.items():
                emit_straight_run(pipes, col * STRIDE, wire_y(row), 0, z_offsets[0], 0)
        if num_layers > 0 and blocks_by_channel.get(num_layers):
            z_tail = z_offsets[-1] + layer_depths[-1]
            tail = dict(cols(num_layers - 1, "out"))
            tail.update(program.parking[-1])
            for qubit, (row, col) in tail.items():
                emit_straight_run(pipes, col * STRIDE, wire_y(row), z_tail, z_end, program.exit_colors.get(qubit, 0))
        if num_layers == 0:
            for qubit in range(program.num_qubits):
                emit_straight_run(pipes, (qubit + 1) * STRIDE, WIRE_J, 0, z_end, 0)

        for index, macro in enumerate(injections):
            channel = macro.layer
            qubit = macro.qubits[0]
            if channel == 0 or num_layers == 0:
                parity = 0
            elif channel >= num_layers:
                parity = program.exit_colors.get(qubit, 0)
            else:
                parity = colors.get(channel - 1, {}).get(qubit, 0)
            if die_dims is not None:
                emit_die_injection(pipes, macro.offset, macro.row, die_dims[0], injection_z0[index], parity, slot=macro.slot)
            else:
                emit_injection(pipes, macro.offset, program.magic_column, injection_z0[index], parity, slot=macro.slot)

    pipes.sort(key=lambda pipe: (pipe["lo"], pipe["hi"], pipe["axis"]))
    bbox = [0, 0, 0]
    for pipe in pipes:
        for coordinate in range(3):
            bbox[coordinate] = max(bbox[coordinate], pipe["hi"][coordinate])
    bbox[2] = max(bbox[2], z_end)

    num_ports = 2 * program.num_qubits
    cubes = {tuple(pipe["lo"]) for pipe in pipes} | {tuple(pipe["hi"]) for pipe in pipes}
    tiles = [(max(cube[coordinate] for cube in cubes) - min(cube[coordinate] for cube in cubes)) // 2 + 1 for coordinate in range(3)] if cubes else [0, 0, 0]

    layout = {"pipes": pipes, "volume": bbox[0] * bbox[1] * bbox[2], "occupied_volume": len(cubes) + len(pipes), "cube_envelope": tiles[0] * tiles[1] * tiles[2], "bbox": bbox, "route_stats": {"nets_unrouted": 0, "feedthroughs_unrouted": 0, "ports_total": num_ports, "surface_pins_routed": num_ports}}
    if injections:
        layout["compute_volume"] = layout["volume"]
        factory_boxes = []
        if factory is not None:
            span = factory.interval_k * STRIDE
            for index, macro in enumerate(injections):
                z_zz = injection_z0[index] + STRIDE
                if die_dims is not None:
                    face = die_dims[0] * STRIDE
                    strip = injection_factory[index] * factory.dim_j * STRIDE
                else:
                    face = (program.magic_column + 2 * INJECTION_BANDWIDTH - 1) * STRIDE
                    strip = injection_factory[index] * factory.dim_j * STRIDE
                factory_boxes.append({"lo": [face, strip, z_zz - span], "hi": [face + factory.dim_i * STRIDE, strip + factory.dim_j * STRIDE, z_zz]})
            full = [max(bbox[coordinate], *(box["hi"][coordinate] for box in factory_boxes)) for coordinate in range(3)]
            layout["volume"] = full[0] * full[1] * full[2]
        layout["occupied_volume"] += sum((box["hi"][0] - box["lo"][0]) * (box["hi"][1] - box["lo"][1]) * (box["hi"][2] - box["lo"][2]) for box in factory_boxes)
        layout["factory_boxes"] = factory_boxes
        layout["t_count"] = len(injections)
        layout["wait_levels"] = wait_levels

    return layout

_UNIT_POSITIONS = ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))
_UNIT_INDICES = (0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 0, 5, 1, 0, 4, 5, 3, 2, 6, 3, 6, 7, 0, 3, 7, 0, 7, 4, 1, 5, 6, 1, 6, 2)


PIPE_AXIS_SCALE = 1.4
PIPE_CROSS_SCALE = 0.55


def _collect_pipe_boxes(pipes):
    entries = []
    for pipe in pipes:
        low, high = tuple(pipe["lo"]), tuple(pipe["hi"])
        middle = tuple((low[coordinate] + high[coordinate]) // 2 for coordinate in range(3))
        if pipe.get("hadamard"):
            material = 3
        elif pipe.get("t_volume"):
            material = 4
        else:
            material = 1 if pipe["parity"] >= 1 else 0
        entries.append((low, material, middle, pipe["axis"]))
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return [(middle, material, axis) for _low, material, middle, axis in entries]


def _pipe_node(middle, material, axis):
    axis_index = {"I": 0, "J": 1, "K": 2}[axis]
    scale = [PIPE_CROSS_SCALE] * 3
    scale[axis_index] = PIPE_AXIS_SCALE
    margin = (1.0 - PIPE_CROSS_SCALE) / 2
    translation = [float(coordinate) + margin for coordinate in middle]
    translation[axis_index] = float(middle[axis_index]) - (PIPE_AXIS_SCALE - 1.0) / 2
    return {"mesh": material, "translation": translation, "scale": scale}


def _collect_cubes(pipes):
    positions = set()
    for pipe in pipes:
        positions.add(tuple(pipe["lo"]))
        positions.add(tuple(pipe["hi"]))
    return sorted(positions)


def _pack_shared_mesh():
    position_bytes = struct.pack("<24f", *(value for vertex in _UNIT_POSITIONS for value in vertex))
    index_bytes = struct.pack("<36H", *_UNIT_INDICES)
    buffer = bytearray(position_bytes)
    index_offset = len(buffer)
    buffer += index_bytes
    while len(buffer) % 4 != 0:
        buffer.append(0)
    return bytes(buffer), len(position_bytes), index_offset, len(index_bytes)


def write_gltf(layout, path):
    """Render a built layout as a glTF 2.0 scene file.

    Writes JSON glTF (materials: red/blue parity, grey cubes, yellow
    hadamard, green T-volume/factory).

    :param layout: dict from :func:`build_layout`.
    :param path: str | Path of the output ``.gltf``.
    :returns: None.
    """
    pipes = layout["pipes"]
    pipe_boxes = _collect_pipe_boxes(pipes)
    cubes = _collect_cubes(pipes)
    buffer, position_bytes_length, index_offset, index_bytes_length = _pack_shared_mesh()
    has_t_volume = any(pipe.get("t_volume") for pipe in pipes) or bool(layout.get("factory_boxes"))

    root = {
        "asset": {"version": "2.0", "generator": "miniflash"},
        "buffers": [{"byteLength": len(buffer), "uri": "data:application/octet-stream;base64," + base64.b64encode(buffer).decode("ascii")}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": position_bytes_length, "target": 34962},
            {"buffer": 0, "byteOffset": index_offset, "byteLength": index_bytes_length, "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "byteOffset": 0, "componentType": 5126, "count": 8, "type": "VEC3", "min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 1.0]},
            {"bufferView": 1, "byteOffset": 0, "componentType": 5123, "count": 36, "type": "SCALAR"},
        ],
        "materials": [
            {"pbrMetallicRoughness": {"baseColorFactor": [0.85, 0.15, 0.15, 1.0]}},
            {"pbrMetallicRoughness": {"baseColorFactor": [0.15, 0.15, 0.85, 1.0]}},
            {"pbrMetallicRoughness": {"baseColorFactor": [0.55, 0.55, 0.55, 1.0]}},
            {"pbrMetallicRoughness": {"baseColorFactor": [0.95, 0.78, 0.10, 1.0]}},
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": material}]} for material in range(4)],
    }
    if has_t_volume:
        root["materials"].append({"pbrMetallicRoughness": {"baseColorFactor": [0.15, 0.75, 0.25, 1.0]}})
        root["meshes"].append({"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 4}]})

    nodes = []
    for middle, material, axis in pipe_boxes:
        nodes.append(_pipe_node(middle, material, axis))
    for position in cubes:
        nodes.append({"mesh": 2, "translation": [float(coordinate) for coordinate in position], "scale": [1.0, 1.0, 1.0]})
    for factory_box in layout.get("factory_boxes") or []:
        scale = [float(factory_box["hi"][coordinate] - factory_box["lo"][coordinate]) for coordinate in range(3)]
        nodes.append({"mesh": 4, "translation": [float(coordinate) for coordinate in factory_box["lo"]], "scale": scale})

    root["nodes"] = nodes
    root["scenes"] = [{"nodes": list(range(len(nodes)))}]
    root["scene"] = 0

    with open(path, "w") as file:
        json.dump(root, file, indent=2)
        file.write("\n")
