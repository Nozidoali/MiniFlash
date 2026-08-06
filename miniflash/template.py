"""Template synthesis: deterministic per-gate block generation.

:func:`template_cell` realizes a region without any solver: every gate is
a fixed local block pattern written directly in Cell geometry and stacked
serially along K. The only contract is K-in K-out — wires enter each
stage from below and leave above at the same columns — and the pin
parity is whatever the wire ledger says.

Primitive algebra: a hadamard wall (1 K unit) applies logical H and
flips the patch orientation, welded together; a rotation (the 7-turn
corkscrew below, 3 cube planes) flips orientation with no logical
action; a bare temporal end declares its init/measure basis freely; a
staple merges two columns through the j=+1 jog row. Gate patterns follow
the Pauli-product reading. ``h`` is one wall, flipping the ledger bit.
``s``/``sdg`` are a Y-staple — via to a ynode (Y init) whose top face is
declared an X measurement; their unsigned stabilizer flow is identical
and the sign lives in the Pauli frame. ``cx`` is the L staple: M_ZZ at
the control via, the bridge riding the jog row, M_XX at the target via
two planes later (the merges anticommute and must be sequential); the
bridge's temporal caps are declared |+> init and M_Z. ``x`` is
frame-only. Attachment axes follow the face table — bit 0 puts Z on the
I faces and X on the J faces, bit 1 swaps — so a via crossing a basis
boundary carries a spatial wall: at the control iff its bit is 0, at the
target iff its bit is 1, and never anywhere else.

Init/measure cap bases are construction semantics with no Cell
representation (matching the rest of the codebase, which stores geometry
and parities only); ynode blocks are the one represented cap (Y).
"""
from .synthesis import PARITY_NONE, Cell, _assert_coords_in_bounds, _dual_stabilizers, _effective_gates, _unsigned_stabilizers_and_fixup

WIRE_J = 2
HOP_J = 3
JOG_J = 4


def _pipe(i, j, k, axis, parity):
    return {"kind": "pipe", "pos": [i, j, k], "axis": axis, "parity": parity}


def _via(column, k, bit, walled):
    kind = "hadamard" if walled else "pipe"
    return {"kind": kind, "pos": [2 * column, HOP_J, k], "axis": "J", "parity": 1 - bit}


def _h_stage(columns, gate_qubits, bits):
    qubit = gate_qubits[0]
    column = columns[qubit]
    return 3, {column}, [{"kind": "hadamard", "pos": [2 * column, WIRE_J, 1], "axis": "K", "parity": bits[qubit]}], {qubit: 1 - bits[qubit]}


def _s_stage(columns, gate_qubits, bits):
    qubit = gate_qubits[0]
    column = columns[qubit]
    bit = bits[qubit]
    blocks = [
        _pipe(2 * column, WIRE_J, 1, "K", bit),
        _via(column, 2, bit, walled=bit == 0),
        {"kind": "ynode", "pos": [2 * column, JOG_J, 2], "parity": PARITY_NONE},
    ]
    return 3, {column}, blocks, {}


def _cx_stage(columns, gate_qubits, bits):
    control_qubit, target_qubit = gate_qubits
    control, target = columns[control_qubit], columns[target_qubit]
    control_bit, target_bit = bits[control_qubit], bits[target_qubit]
    step = 2 if target > control else -2
    blocks = [
        _pipe(2 * control, WIRE_J, 1, "K", control_bit),
        _pipe(2 * control, WIRE_J, 3, "K", control_bit),
        _pipe(2 * target, WIRE_J, 1, "K", target_bit),
        _pipe(2 * target, WIRE_J, 3, "K", target_bit),
        _via(control, 2, control_bit, walled=control_bit == 0),
        _pipe(2 * target, JOG_J, 3, "K", control_bit),
        _via(target, 4, target_bit, walled=target_bit == 1),
    ]
    for i in range(2 * control, 2 * target, step):
        blocks.append(_pipe(i + step // 2, JOG_J, 2, "I", control_bit))
    return 5, {control, target}, blocks, {}


def _rotation_stage(columns, gate_qubits, bits):
    qubit = gate_qubits[0]
    column = columns[qubit]
    bit = bits[qubit]
    base = 2 * column
    blocks = [
        _pipe(base, HOP_J, 0, "J", 1 - bit),
        _pipe(base + 1, JOG_J, 0, "I", bit),
        _pipe(base + 2, JOG_J, 1, "K", bit),
        _pipe(base + 1, JOG_J, 2, "I", bit),
        _pipe(base, JOG_J, 3, "K", bit),
        _pipe(base, HOP_J, 4, "J", bit),
    ]
    return 5, {column}, blocks, {qubit: 1 - bit}


_STAGES = {"h": _h_stage, "s": _s_stage, "sdg": _s_stage, "cx": _cx_stage, "rotate": _rotation_stage}


def template_cell(region, in_columns=None, in_bases=None) -> Cell:
    """Compose a region's cell from deterministic per-gate block patterns.

    :param region: Region whose whole-cell SAT attempt is being replaced.
    :param in_columns: {qubit: local column}; the composed cell keeps the
        same columns at entry and exit.
    :param in_bases: {qubit: 0|1} wire parity at entry.
    :returns: Cell (``pauli_frame`` and ``in_bases`` set like the SAT path).
    :raises ValueError: on a gate with no template.
    """
    columns = dict(in_columns) if in_columns is not None else {qubit: index + 1 for index, qubit in enumerate(region.qubits)}
    bits = {qubit: (in_bases or {}).get(qubit, 0) for qubit in region.qubits}
    entry_bits = dict(bits)

    blocks = []
    pins = [{"name": region.in_pins[qubit], "dir": "K", "end": "-", "parity": bits[qubit], "offset": [2 * columns[qubit], WIRE_J, -1]} for qubit in region.qubits]
    offset = 0
    for name, gate_qubits in region.gates:
        if name == "x":
            continue
        if name not in _STAGES:
            raise ValueError(f"template_cell: unsupported gate {name!r}")
        depth, involved_columns, stage_blocks, flips = _STAGES[name](columns, gate_qubits, bits)

        if offset > 0:
            for qubit in region.qubits:
                blocks.append(_pipe(2 * columns[qubit], WIRE_J, offset - 1, "K", bits[qubit]))
        for block in stage_blocks:
            position = list(block["pos"])
            position[2] += offset
            blocks.append({**block, "pos": position})
        for qubit in region.qubits:
            if columns[qubit] not in involved_columns:
                for k in range(1, depth, 2):
                    blocks.append(_pipe(2 * columns[qubit], WIRE_J, offset + k, "K", bits[qubit]))
        bits.update(flips)
        offset += depth + 1

    height = offset - 1 if offset else 1
    for qubit in region.qubits:
        pins.append({"name": region.out_pins[qubit], "dir": "K", "end": "+", "parity": bits[qubit], "offset": [2 * columns[qubit], WIRE_J, height]})

    seen = set()
    for block in blocks:
        key = tuple(block["pos"])
        if key in seen:
            raise RuntimeError(f"template_cell: block collision at {key}")
        seen.add(key)

    width = max(len(region.qubits) + 2, max(columns.values()) + 2)
    dims = [2 * width - 1, 5, height]
    _assert_coords_in_bounds(blocks, dims)
    duals = _dual_stabilizers(_effective_gates(region, entry_bits), len(region.qubits))
    _, frame = _unsigned_stabilizers_and_fixup(duals)
    return Cell(dims=dims, pins=pins, blocks=blocks, pauli_frame=frame, in_bases=entry_bits)
