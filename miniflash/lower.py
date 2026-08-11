"""IR lowering: Program to measured geometry.

:func:`check_program` validates the IR; :func:`build_layout` plans the
layout (z accounting, banks, injection placement) and folds the streamed
pipe geometry into volume metrics (``volume``, ``cube_envelope``) without
materializing it. :mod:`miniflash.gltf` collects the same stream into real
pipes and renders the scene.
"""

from dataclasses import dataclass
from typing import NamedTuple

from .synthesis import AXIS_INDEX, STRIDE

WIRE_J = STRIDE
INJECTION_BLOCK_LEVELS = 2
ROW_PITCH = 6


class _Pipe(NamedTuple):
    #: low cube endpoint
    lo: tuple
    #: high cube endpoint
    hi: tuple
    #: "I" | "J" | "K"
    axis: str
    #: color bit for rendering
    parity: int
    #: Hadamard wall on this segment
    hadamard: bool = False
    #: magic-state volume
    t_volume: bool = False
    #: side-face routing
    side: bool = False
    #: owning injection index, None for plain geometry
    inj: object = None


def _make_pipe(low, high, axis, parity, **flags):
    return _Pipe(lo=tuple(low), hi=tuple(high), axis=axis, parity=int(parity), **flags)


class _Extent:
    __slots__ = ("hi_max", "end_min", "end_max", "count")

    def __init__(self):
        self.hi_max, self.end_min, self.end_max, self.count = [0, 0, 0], None, None, 0

    def add(self, pipe):
        low, high = pipe.lo, pipe.hi
        if self.end_min is None:
            self.end_min, self.end_max = list(low), list(low)
        for axis in range(3):
            if high[axis] > self.hi_max[axis]:
                self.hi_max[axis] = high[axis]
            for value in (low[axis], high[axis]):
                if value < self.end_min[axis]:
                    self.end_min[axis] = value
                elif value > self.end_max[axis]:
                    self.end_max[axis] = value
        self.count += 1


def _row_base(bases, row):
    return bases[row] if row < len(bases) else row * ROW_PITCH

def _check_nets(program):
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
    return errors

def _check_channels(program):
    errors = []
    nets_by_channel = {}
    for net in program.nets:
        nets_by_channel.setdefault(net.channel, []).append(net)
    for channel in program.channels:
        moves = [net for net in nets_by_channel.get(channel.index, ()) if net.track >= 0]
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
    return errors

def _check_die(program):
    if program.die_dims is None:
        return []
    errors = []
    width = program.die_dims[0]
    for macro in program.macros:
        if macro.kind == "cell" and macro.offset + len([p for p in macro.ref.pins if p["name"].startswith("in")]) + 2 > width:
            errors.append(f"macro layer {macro.layer} offset {macro.offset} exceeds die width {width}")
    return errors

def _check_injections(program):
    errors = []
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
            blocks.setdefault((macro.layer, macro.round, macro.row), []).append(macro)
    factories_total = program.factories
    paced = program.factory is not None and program.factory.interval_k > 0
    rows_split = {}
    if program.die_dims is not None and program.rows > 1 and factories_total >= program.rows:
        per_row, extra = divmod(factories_total, program.rows)
        rows_split = {r: per_row + (1 if r < extra else 0) for r in range(program.rows)}
    for (channel, round_index, row), members in blocks.items():
        cap = rows_split.get(row, max(1, factories_total))
        if paced and len(members) > cap:
            errors.append(f"injection block channel {channel} round {round_index} row {row}: {len(members)} members > capacity {cap}")
        if len({member.qubits[0] for member in members}) != len(members):
            errors.append(f"injection block channel {channel} round {round_index} row {row}: duplicate qubit")
        if len({member.slot for member in members}) != len(members):
            errors.append(f"injection block channel {channel} round {round_index} row {row}: duplicate slot")
    return errors

def check_program(program):
    """Check Program IR consistency before lowering.

    Net endpoints must match cell pins, move levels must fit channel tracks,
    1-D jogs must not overlap on a (level, plane) corridor, die-mode macros
    must fit the die width.

    :param program: Program.
    :returns: list[str] error messages (empty = valid).
    """
    return _check_nets(program) + _check_channels(program) + _check_die(program) + _check_injections(program)

@dataclass
class _Bank:
    #: injection indexes delivered through the backside strips
    backside: set
    #: {factory unit: strip J plane}
    unit_strip: dict
    #: I coordinate of the bank face
    bank_x: int
    #: per-row J base (band + bank pitch where reserved)
    bases: list
    #: {row: [factory units]} in multi-row dies, {} otherwise
    unit_pool: dict

def _bank_of(program, injections, blocks_by_channel):
    from .floorplan import INJECTION_BANDWIDTH

    die_dims = program.die_dims
    factory = program.factory
    factories = max(1, program.factories)
    unit_pool = {}
    if die_dims is not None and program.rows > 1 and factories >= program.rows:
        per_row, extra = divmod(factories, program.rows)
        start = 0
        for row in range(program.rows):
            size = per_row + (1 if row < extra else 0)
            unit_pool[row] = list(range(start, start + size))
            start += size
    rows_total = program.rows if (die_dims is not None and program.rows > 1) else 1
    group_of = {}
    for channel, rounds in blocks_by_channel.items():
        for round_index, members in rounds.items():
            for member in members:
                group_of.setdefault((channel, round_index, injections[member].row), []).append(member)
    backside = {member for members in group_of.values() if len(members) > INJECTION_BANDWIDTH for member in members}
    backside_rows = {injections[member].row for member in backside}
    bank_planes = factory.dim_j * STRIDE if factory is not None else 0
    units_of_row = {row: len(units) for row, units in unit_pool.items()} if unit_pool else ({0: factories} if factory is not None else {})
    bases, base = [], 0
    for row in range(rows_total):
        bases.append(base)
        base += 3 * STRIDE + (units_of_row.get(row, 0) * bank_planes if row in backside_rows else 0)
    unit_strip = {}
    pools = unit_pool if unit_pool else ({0: list(range(factories))} if factory is not None else {})
    for row, units in pools.items():
        for local, unit in enumerate(units):
            unit_strip[unit] = bases[row] + 3 * STRIDE + local * bank_planes
    if die_dims is not None:
        bank_cols = die_dims[0]
    else:
        bank_cols = max([*(program.box_widths or [0]), program.num_qubits + 1]
                        + [col + 1 for layer in program.parking for _, col in layer.values()]
                        + [macro.offset + 1 for macro in injections])
    return _Bank(backside=backside, unit_strip=unit_strip, bank_x=bank_cols * STRIDE, bases=bases, unit_pool=unit_pool)

def _assign_units(members, injections, factory, factories, unit_pool, last_zz, candidate_zz):
    span = factory.interval_k * STRIDE
    ready = [value + span for value in last_zz]
    chosen = []
    shortfall = 0
    for member_index in members:
        pool = unit_pool.get(injections[member_index].row) if unit_pool else None
        candidates = pool if pool else range(factories)
        free = [machine for machine in candidates if machine not in chosen] or list(candidates)
        unit = min(free, key=lambda machine: ready[machine])
        shortfall = max(shortfall, ready[unit] - candidate_zz)
        ready[unit] += span
        chosen.append(unit)
    return chosen, shortfall

@dataclass
class _ZPlan:
    #: per layer, K offset of the cell band
    z_offsets: list
    #: K extent of the whole layout
    z_end: int
    #: idle K levels inserted waiting for factory output
    wait_levels: int
    #: per injection, K offset of its crossbar block
    injection_z0: list
    #: per injection, the factory unit that feeds it
    injection_factory: list

def _zplan_of(program, layers, layer_depths, injections, blocks_by_channel, in_gap_by_boundary, bank):
    factory = program.factory
    factories = max(1, program.factories)
    gap_heights = [(channel.tracks + 1) * STRIDE for channel in program.channels]
    injection_z0 = [None] * len(injections)
    injection_factory = [0] * len(injections)
    last_zz = [0] * factories

    def _place_rounds(items, z_run):
        waits = 0
        for _, members in items:
            candidate_zz = z_run + STRIDE
            if factory is not None:
                span = factory.interval_k * STRIDE
                chosen, shortfall = _assign_units(members, injections, factory, factories, bank.unit_pool, last_zz, candidate_zz)
                if shortfall > 0:
                    wait = -(-shortfall // STRIDE) * STRIDE
                    z_run += wait
                    waits += wait // STRIDE
                    candidate_zz = z_run + STRIDE
                counts = {}
                pairing = zip(sorted(members, key=lambda m: injections[m].offset, reverse=True), sorted(chosen))
                for member_index, unit in pairing:
                    injection_factory[member_index] = unit
                    counts[unit] = counts.get(unit, 0) + 1
                for unit, count in counts.items():
                    last_zz[unit] = candidate_zz + span * (count - 1) if count > 1 else candidate_zz
            for member_index in members:
                injection_z0[member_index] = z_run
            z_run += INJECTION_BLOCK_LEVELS * STRIDE
        return z_run, waits

    def _place_blocks(channel, z):
        rounds = blocks_by_channel.get(channel, {})
        if not rounds:
            return z, 0
        if program.die_dims is not None and bank.unit_pool:
            z_stop, waits = z, 0
            for row in sorted({injections[m].row for ms in rounds.values() for m in ms}):
                items = [(r, [m for m in rounds[r] if injections[m].row == row]) for r in sorted(rounds)]
                z_row, row_waits = _place_rounds([(r, ms) for r, ms in items if ms], z)
                z_stop, waits = max(z_stop, z_row), waits + row_waits
            return z_stop, waits
        return _place_rounds([(r, rounds[r]) for r in sorted(rounds)], z)

    num_layers = len(layers)
    z_offsets, z, wait_levels = [], 0, 0
    z, waits = _place_blocks(0, z)
    wait_levels += waits
    for layer in range(num_layers):
        z_offsets.append(z)
        z += layer_depths[layer]
        if layer < num_layers - 1:
            z += gap_heights[layer]
            z, waits = _place_blocks(layer + 1, z)
            wait_levels += waits
    if num_layers > 0:
        z, waits = _place_blocks(num_layers, z)
        wait_levels += waits

    for boundary, indexes in in_gap_by_boundary.items():
        z_top = z_offsets[boundary] + layer_depths[boundary]
        for index in indexes:
            injection_z0[index] = z_top + injections[index].level * STRIDE

    return _ZPlan(z_offsets=z_offsets, z_end=z, wait_levels=wait_levels, injection_z0=injection_z0, injection_factory=injection_factory)

@dataclass
class _Plan:
    #: the Program being lowered
    program: object
    #: number of cell layers
    num_layers: int
    #: (width, rows | None) die constraint, None for 1-D
    die_dims: tuple
    #: per layer, cell macros
    layers: list
    #: FactorySpec | None
    factory: object
    #: injection macros in program order
    injections: list
    #: {channel: {qubit: color bit}}
    colors: dict
    #: {channel: [Net]}
    nets_by_channel: dict
    #: per layer, K depth of the tallest cell
    layer_depths: list
    #: {channel: {round: [injection indexes]}} for block injections
    blocks_by_channel: dict
    #: {boundary: [injection indexes]} for gap-riding injections
    in_gap_by_boundary: dict
    #: backside/bank geometry
    bank: _Bank
    #: K accounting
    zplan: _ZPlan

    @property
    def bases(self):
        return self.bank.bases

    @property
    def backside(self):
        return self.bank.backside

    @property
    def unit_strip(self):
        return self.bank.unit_strip

    @property
    def bank_x(self):
        return self.bank.bank_x

    @property
    def z_offsets(self):
        return self.zplan.z_offsets

    @property
    def z_end(self):
        return self.zplan.z_end

    @property
    def wait_levels(self):
        return self.zplan.wait_levels

    @property
    def injection_z0(self):
        return self.zplan.injection_z0

    @property
    def injection_factory(self):
        return self.zplan.injection_factory

    def bits_at(self, channel):
        return self.program.exit_colors if channel >= self.num_layers - 1 else self.colors.get(channel, {})

    def cols(self, layer, prefix):
        slots = {}
        for macro in self.layers[layer]:
            for pin in macro.ref.pins:
                if pin["name"].startswith(prefix) and pin["dir"] == "K":
                    slots[macro.qubits[int(pin["name"].rsplit("_", 1)[1])]] = (macro.row, macro.offset + pin["offset"][0] // 2)
        return slots

def _plan(program):
    problems = check_program(program)
    if problems:
        raise RuntimeError(f"build_layout: IR check failed: {problems[:3]}")

    num_layers = len(program.box_widths)
    layers = [[] for _ in range(num_layers)]
    for macro in program.macros:
        if macro.kind == "cell":
            layers[macro.layer].append(macro)
    injections = [macro for macro in program.macros if macro.kind == "injection"]
    colors = {}
    nets_by_channel = {}
    for net in program.nets:
        colors.setdefault(net.channel, {})[net.qubit] = net.color
        nets_by_channel.setdefault(net.channel, []).append(net)
    layer_depths = [max(macro.ref.dims[2] - 1 for macro in layer) for layer in layers]

    blocks_by_channel = {}
    in_gap_by_boundary = {}
    for index, macro in enumerate(injections):
        if macro.level >= 0:
            in_gap_by_boundary.setdefault(macro.layer - 1, []).append(index)
        else:
            blocks_by_channel.setdefault(macro.layer, {}).setdefault(macro.round, []).append(index)

    bank = _bank_of(program, injections, blocks_by_channel)
    zplan = _zplan_of(program, layers, layer_depths, injections, blocks_by_channel, in_gap_by_boundary, bank)
    return _Plan(program=program, num_layers=num_layers, die_dims=program.die_dims, layers=layers,
                 factory=program.factory, injections=injections, colors=colors,
                 nets_by_channel=nets_by_channel, layer_depths=layer_depths,
                 blocks_by_channel=blocks_by_channel, in_gap_by_boundary=in_gap_by_boundary,
                 bank=bank, zplan=zplan)

class _Emitter:
    def __init__(self, program, plan):
        self.program = program
        self.plan = plan

    def row_base(self, row):
        return _row_base(self.plan.bases, row)

    def wire_y(self, row):
        return self.row_base(row) + WIRE_J

    def slot_plane(self, row, slot):
        return self.row_base(row) + (2 * STRIDE if slot == 0 else 0)

    def straight_run(self, column, plane_j, z_start, z_end, parity_bit):
        for z in range(z_start, z_end, STRIDE):
            yield _make_pipe([column, plane_j, z], [column, plane_j, z + STRIDE], "K", parity_bit)

    def flip_landing(self, column, z_start, z_bottom, parity_bit):
        yield from self.straight_run(column, WIRE_J, z_start, z_bottom - STRIDE, parity_bit)
        yield _make_pipe([column, WIRE_J, z_bottom - STRIDE], [column, WIRE_J, z_bottom], "K", parity_bit, hadamard=True)

    def j_run(self, x, y_start, y_end, z, parity_bit):
        for y in range(min(y_start, y_end), max(y_start, y_end), STRIDE):
            yield _make_pipe([x, y, z], [x, y + STRIDE, z], "J", parity_bit)

    def cell_pipes(self, blocks, z_offset, x_offset=0, y_offset=0):
        for block in blocks:
            if block["kind"] not in ("pipe", "hadamard"):
                continue
            position = [block["pos"][0] + x_offset, block["pos"][1] + y_offset, block["pos"][2] + z_offset]
            low, high = list(position), list(position)
            axis_index = AXIS_INDEX[block["axis"]]
            low[axis_index] -= 1
            high[axis_index] += 1
            yield _make_pipe(low, high, block["axis"], block.get("parity", 0), hadamard=block["kind"] == "hadamard")

    def _waypoints(self, move):
        (row_a, col_a), (row_b, col_b) = move.src, move.dst
        points = [(col_a, self.wire_y(row_a))]
        column = col_a
        for segment in move.path:
            if segment[0] != "I":
                continue
            _, row, plane, low, high = segment
            j = self.row_base(row) + plane * STRIDE
            target = low if column == high else high
            points += [(column, j), (target, j)]
            column = target
        points.append((col_b, self.wire_y(row_b)))
        return [point for index, point in enumerate(points) if index == 0 or point != points[index - 1]]

    def grid_path(self, move, jog_z, parity_bit):
        hop_bit = 1 - parity_bit
        points = self._waypoints(move)
        for (col_a, y_a), (col_b, y_b) in zip(points, points[1:]):
            if y_a == y_b:
                step = STRIDE if col_b > col_a else -STRIDE
                for x in range(col_a * STRIDE, col_b * STRIDE, step):
                    low, high = sorted((x, x + step))
                    yield _make_pipe([low, y_a, jog_z], [high, y_a, jog_z], "I", parity_bit)
            else:
                yield from self.j_run(col_a * STRIDE, y_a, y_b, jog_z, hop_bit)

    def side_face_run(self, face_cube, hop_cube, z_port, parity_bit, flip):
        for i in range(face_cube, hop_cube, STRIDE):
            first = flip and i == face_cube
            yield _make_pipe([i, WIRE_J, z_port], [i + STRIDE, WIRE_J, z_port], "I", 0 if first else parity_bit, side=True, hadamard=first)

    def side_exit(self, face_cube, hop_cube, lane_cube, plane_j, z_port, z_bottom, parity_bit):
        yield from self.side_face_run(face_cube, hop_cube, z_port, parity_bit, False)
        hop_bit = 1 - parity_bit
        yield _make_pipe([hop_cube, min(WIRE_J, plane_j), z_port], [hop_cube, max(WIRE_J, plane_j), z_port], "J", hop_bit, side=True)
        for i in range(hop_cube, lane_cube, STRIDE):
            yield _make_pipe([i, plane_j, z_port], [i + STRIDE, plane_j, z_port], "I", parity_bit, side=True)
        yield _make_pipe([lane_cube, min(WIRE_J, plane_j), z_port], [lane_cube, max(WIRE_J, plane_j), z_port], "J", hop_bit, side=True)
        for z in range(z_port + 1, z_bottom, STRIDE):
            yield _make_pipe([lane_cube, WIRE_J, z], [lane_cube, WIRE_J, z + STRIDE], "K", parity_bit, side=True)

    def side_entry(self, face_cube, hop_cube, lane_cube, plane_j, z_start, z_port, parity_bit, flip):
        hop_bit = 1 - parity_bit
        for z in range(z_start, z_port, STRIDE):
            yield _make_pipe([lane_cube, WIRE_J, z], [lane_cube, WIRE_J, z + STRIDE], "K", parity_bit, side=True)
        yield _make_pipe([lane_cube, min(WIRE_J, plane_j), z_port], [lane_cube, max(WIRE_J, plane_j), z_port], "J", hop_bit, side=True)
        for i in range(hop_cube, lane_cube, STRIDE):
            yield _make_pipe([i, plane_j, z_port], [i + STRIDE, plane_j, z_port], "I", parity_bit, side=True)
        yield _make_pipe([hop_cube, min(WIRE_J, plane_j), z_port], [hop_cube, max(WIRE_J, plane_j), z_port], "J", hop_bit, side=True)
        yield from self.side_face_run(face_cube, hop_cube, z_port, parity_bit, flip)

    def pipes(self):
        plan, program = self.plan, self.program
        for layer in range(plan.num_layers):
            out_slots = plan.cols(layer, "out")
            for macro in plan.layers[layer]:
                yield from self.cell_pipes(macro.ref.blocks, plan.z_offsets[layer], macro.offset * STRIDE, self.row_base(macro.row))
            for macro in plan.layers[layer]:
                depth = macro.ref.dims[2] - 1
                if depth < plan.layer_depths[layer]:
                    for qubit in macro.qubits:
                        row, col = out_slots[qubit]
                        yield from self.straight_run(col * STRIDE, self.wire_y(row), plan.z_offsets[layer] + depth, plan.z_offsets[layer] + plan.layer_depths[layer], plan.bits_at(layer).get(qubit, 0))
            for qubit, (row, col) in program.parking[layer].items():
                parity = plan.colors.get(layer - 1, {}).get(qubit, 0) if layer > 0 else 0
                yield from self.straight_run(col * STRIDE, self.wire_y(row), plan.z_offsets[layer], plan.z_offsets[layer] + plan.layer_depths[layer], parity)

        for channel in range(plan.num_layers - 1):
            z_top = plan.z_offsets[channel] + plan.layer_depths[channel]
            z_bottom = plan.z_offsets[channel + 1]
            bits = plan.bits_at(channel)

            yield from self.channel_pipes(channel, z_top, z_bottom, bits)

            for index in plan.in_gap_by_boundary.get(channel, ()):
                macro = plan.injections[index]
                for pipe in self.crossbar(macro, plan.injection_z0[index], bits.get(macro.qubits[0], 0)):
                    yield pipe._replace(inj=index)

            if plan.die_dims is None and program.sides:
                exit_face_cube = (program.box_widths[channel] - 1) * STRIDE
                hop_cube = max(program.box_widths) * STRIDE
                for qubit, (lane_column, slot, plane) in program.sides[channel][0].items():
                    z_port = plan.z_offsets[channel] + plan.layer_depths[channel] - 2 * slot
                    yield from self.side_exit(exit_face_cube, hop_cube, lane_column * STRIDE, plane * STRIDE, z_port, z_bottom, 0)
                entry_face_cube = (program.box_widths[channel + 1] - 1) * STRIDE
                for qubit, (lane_column, slot, plane) in program.sides[channel + 1][1].items():
                    z_port = z_bottom + 2 * slot
                    yield from self.side_entry(entry_face_cube, hop_cube, lane_column * STRIDE, plane * STRIDE, z_bottom, z_port, bits.get(qubit, 0), bits.get(qubit, 0) == 1)

        if plan.injections:
            if plan.num_layers > 0 and plan.z_offsets[0] > 0:
                head = dict(plan.cols(0, "in"))
                head.update(program.parking[0])
                for qubit, (row, col) in head.items():
                    yield from self.straight_run(col * STRIDE, self.wire_y(row), 0, plan.z_offsets[0], 0)
            if plan.num_layers > 0 and plan.blocks_by_channel.get(plan.num_layers):
                z_tail = plan.z_offsets[-1] + plan.layer_depths[-1]
                tail = dict(plan.cols(plan.num_layers - 1, "out"))
                tail.update(program.parking[-1])
                for qubit, (row, col) in tail.items():
                    yield from self.straight_run(col * STRIDE, self.wire_y(row), z_tail, plan.z_end, program.exit_colors.get(qubit, 0))
            if plan.num_layers == 0:
                for qubit in range(program.num_qubits):
                    yield from self.straight_run((qubit + 1) * STRIDE, WIRE_J, 0, plan.z_end, 0)

            for index, macro in enumerate(plan.injections):
                channel = macro.layer
                qubit = macro.qubits[0]
                if channel == 0 or plan.num_layers == 0:
                    parity = 0
                elif channel >= plan.num_layers:
                    parity = program.exit_colors.get(qubit, 0)
                else:
                    parity = plan.colors.get(channel - 1, {}).get(qubit, 0)
                if index in plan.backside:
                    strip_j = plan.unit_strip.get(plan.injection_factory[index], self.row_base(macro.row) + 3 * STRIDE)
                    crossbar = self.backside_pipes(macro.offset, macro.row, strip_j, plan.injection_z0[index], parity)
                else:
                    crossbar = self.crossbar(macro, plan.injection_z0[index], parity)
                for pipe in crossbar:
                    yield pipe._replace(inj=index)

    def backside_pipes(self, column, row, strip_j, z0, parity_bit):
        """One backside injection: merge stub at z_zz, K riser, z0 lane run to the bank."""
        wire = self.wire_y(row)
        corridor = self.row_base(row) + 2 * STRIDE
        x_d = column * STRIDE
        z_zz = z0 + STRIDE
        yield _make_pipe([x_d, wire, z_zz], [x_d, corridor, z_zz], "J", 1 - parity_bit, t_volume=True)
        yield _make_pipe([x_d, corridor, z0], [x_d, corridor, z_zz], "K", 0, t_volume=True)
        for j in range(corridor, strip_j, STRIDE):
            yield _make_pipe([x_d, j, z0], [x_d, j + STRIDE, z0], "J", 0, t_volume=True)
        for i in range(x_d, self.plan.bank_x, STRIDE):
            yield _make_pipe([i, strip_j, z0], [i + STRIDE, strip_j, z0], "I", 0, t_volume=True)

class _RowEmitter(_Emitter):
    def channel_pipes(self, channel, z_top, z_bottom, bits):
        from .floorplan import GridMove

        for net in self.plan.nets_by_channel.get(channel, ()):
            parity = bits.get(net.qubit, 0)
            z_previous = z_top
            slot = net.source[1:]
            for level, src, dst, *segments in net.path:
                jog_z = z_top + (level + 1) * STRIDE
                yield from self.straight_run(slot[1] * STRIDE, self.wire_y(slot[0]), z_previous, jog_z, parity)
                yield from self.grid_path(GridMove(qubit=net.qubit, src=tuple(src), dst=tuple(dst), level=level, path=tuple(segments)), jog_z, parity)
                slot, z_previous = tuple(dst), jog_z
            if net.flip:
                yield from self.flip_landing(slot[1] * STRIDE, z_previous, z_bottom, parity)
            else:
                yield from self.straight_run(slot[1] * STRIDE, self.wire_y(slot[0]), z_previous, z_bottom, parity)

    def crossbar(self, macro, z0, parity_bit):
        z_zz = z0 + STRIDE
        data_cube = macro.offset * STRIDE
        plane = self.slot_plane(macro.row, macro.slot)
        hop_bit = 1 - parity_bit
        for y in range(min(self.wire_y(macro.row), plane), max(self.wire_y(macro.row), plane), STRIDE):
            yield _make_pipe([data_cube, y, z_zz], [data_cube, y + STRIDE, z_zz], "J", hop_bit, t_volume=True)
        for x in range(data_cube, self.plan.die_dims[0] * STRIDE, STRIDE):
            yield _make_pipe([x, plane, z_zz], [x + STRIDE, plane, z_zz], "I", parity_bit, t_volume=True)

class _LaneEmitter(_Emitter):
    def channel_pipes(self, channel, z_top, z_bottom, bits):
        from .channel import Lane, RearrangementChannel, RearrangementPlan, plan_blocks
        from .floorplan import Move

        nets = self.plan.nets_by_channel.get(channel, [])
        lanes = {}
        for net in nets:
            parity_in = bits.get(net.qubit, 0)
            lanes[net.qubit] = Lane(start=(0, net.source[2]), end=(0, net.sink[2]), parity_in=parity_in, parity_out=0 if net.flip else parity_in)
        moves = tuple(Move(qubit=net.qubit, from_column=from_column, to_column=to_column, level=level, plane=plane) for net in nets for level, from_column, to_column, plane in net.path)
        rearrangement = RearrangementPlan(channel=RearrangementChannel(lanes=lanes), moves=moves, levels=0)
        yield from self.cell_pipes(plan_blocks(rearrangement, z_bottom - z_top), z_top)

    def crossbar(self, macro, z0, parity_bit):
        data_cube = macro.offset * STRIDE
        if self.program.magic_sides == 2:
            magic_cube = self.program.magic_column * STRIDE if macro.slot == 0 else 0
        else:
            magic_cube = (self.program.magic_column + 2 * macro.slot) * STRIDE
        z_zz = z0 + STRIDE
        plane_j = 2 * STRIDE if macro.slot == 0 else 0
        hop_bit = 1 - parity_bit
        if self.program.magic_sides != 2:
            yield _make_pipe([magic_cube, WIRE_J, z0], [magic_cube + STRIDE, WIRE_J, z0], "I", 0, t_volume=True)
        yield _make_pipe([magic_cube, WIRE_J, z0], [magic_cube, WIRE_J, z_zz], "K", 0, t_volume=True)
        yield _make_pipe([data_cube, min(WIRE_J, plane_j), z_zz], [data_cube, max(WIRE_J, plane_j), z_zz], "J", hop_bit, t_volume=True)
        for i in range(min(data_cube, magic_cube), max(data_cube, magic_cube), STRIDE):
            yield _make_pipe([i, plane_j, z_zz], [i + STRIDE, plane_j, z_zz], "I", parity_bit, t_volume=True)
        yield _make_pipe([magic_cube, min(WIRE_J, plane_j), z_zz], [magic_cube, max(WIRE_J, plane_j), z_zz], "J", hop_bit, t_volume=True)

def _pipes_of(program, plan):
    emitter = _RowEmitter(program, plan) if plan.die_dims is not None else _LaneEmitter(program, plan)
    yield from emitter.pipes()

def _factory_boxes(plan):
    if plan.factory is None or not plan.factory.render:
        return []
    span = plan.factory.interval_k * STRIDE
    width = plan.factory.dim_i * STRIDE
    boxes = []
    for index, macro in enumerate(plan.injections):
        z_zz = plan.injection_z0[index] + STRIDE
        if macro.level >= 0 and plan.die_dims is None:
            wire = _row_base(plan.bases, macro.row) + WIRE_J
            if plan.program.magic_sides == 2:
                face = -width if macro.slot == 1 else (plan.program.magic_column + 1) * STRIDE
            else:
                face = (plan.program.magic_column + 2 * macro.slot + 1) * STRIDE
            boxes.append({"lo": [face, wire, z_zz - span], "hi": [face + width, wire + plan.factory.dim_j * STRIDE, z_zz]})
            continue
        face = plan.bank_x
        strip = plan.unit_strip.get(plan.injection_factory[index], _row_base(plan.bases, macro.row) + 3 * STRIDE)
        boxes.append({"lo": [face, strip, z_zz - span], "hi": [face + width, strip + plan.factory.dim_j * STRIDE, z_zz]})
    return boxes

def _layout(program, plan, extent):
    num_ports = 2 * program.num_qubits
    bbox = list(extent.hi_max)
    bbox[2] = max(bbox[2], plan.z_end)
    tiles = [(extent.end_max[coordinate] - extent.end_min[coordinate]) // 2 + 1 for coordinate in range(3)] if extent.count else [0, 0, 0]

    layout = {"volume": bbox[0] * bbox[1] * bbox[2], "cube_envelope": tiles[0] * tiles[1] * tiles[2], "bbox": bbox, "route_stats": {"nets_unrouted": 0, "feedthroughs_unrouted": 0, "ports_total": num_ports, "surface_pins_routed": num_ports}}
    if plan.injections:
        layout["compute_volume"] = layout["volume"]
        factory_boxes = _factory_boxes(plan)
        if factory_boxes:
            full = [max(bbox[coordinate], *(box["hi"][coordinate] for box in factory_boxes)) for coordinate in range(3)]
            low = [min(0, *(box["lo"][coordinate] for box in factory_boxes)) for coordinate in range(3)]
            layout["volume"] = (full[0] - low[0]) * (full[1] - low[1]) * (full[2] - low[2])
        layout["factory_boxes"] = factory_boxes
        layout["t_count"] = len(plan.injections)
        layout["wait_levels"] = plan.wait_levels

    return layout

def build_layout(program):
    """Check a Program and measure its geometry: volumes and extents, no pipes.

    Streams the pipe geometry through an extent fold — O(1) memory;
    :func:`write_gltf` collects the same stream into real pipes.

    :param program: Program.
    :returns: layout dict — volume, cube_envelope, bbox, route_stats; plus
        compute_volume, factory_boxes, t_count and wait_levels when the
        circuit has injections.
    :raises RuntimeError: when the IR check fails.
    """
    plan = _plan(program)
    extent = _Extent()
    for pipe in _pipes_of(program, plan):
        extent.add(pipe)
    return _layout(program, plan, extent)
