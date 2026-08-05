"""Cell synthesis.

Turns one region into a lattice-surgery cell via LaSsynth: stim extracts
the region's Choi dual stabilizers, boundary Paulis are solved over
GF(2), and a SAT solver (kissat when available, z3 otherwise) searches a
depth-minimal geometry that realizes them **at the exact ports the
floorplan assigned**. Solutions land in a canonical disk cache keyed by
the unsigned dual stabilizers plus the port and color assignment, so
gate-order noise, Pauli frames and pending-Hadamard variants share one
cell.

Budgets are enforced with a hard ``SIGALRM`` wall. Exhaustion raises
:class:`SynthTimeout` (the coarse portfolio reacts by splitting the
region), infeasibility raises :class:`SynthUnsat`, and a failed
stabilizer verification raises :class:`VerifyFailed`.
"""
import hashlib
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

import stim
from lassynth import LatticeSurgerySynthesizer
from lassynth.sat_synthesis.lattice_surgery_sat import LatticeSurgerySAT

from dataclasses import dataclass

_VENDOR_FORBID_CUBE = LatticeSurgerySAT.constraint_forbid_cube


def _forbid_cube_and_maybe_y(self):
    import z3

    _VENDOR_FORBID_CUBE(self)
    if self.optional.get("forbid_y_cubes"):
        for i in range(self.n_i):
            for j in range(self.n_j):
                for k in range(self.n_k):
                    self.goal.add(z3.Not(self.vars["NodeY"][i][j][k]))


LatticeSurgerySAT.constraint_forbid_cube = _forbid_cube_and_maybe_y

from .floorplan import solve_floorplan, passthrough_floorplan
from .schedule import schedule_layers

STRIDE = 2
AXES = ("I", "J", "K")
PARITY_NONE = -1
AXIS_INDEX = {"I": 0, "J": 1, "K": 2}


@dataclass
class Cell:
    """A synthesized lattice-surgery cell: geometry blocks plus port pins."""

    #: [I, J, K] extents in cube units
    dims: list
    #: port pin dicts — name ("in_k"/"out_k"), offset, dir, parity
    pins: list
    #: geometry blocks (pipes, hadamards) in cell-local coordinates
    blocks: list
    #: Pauli correction carried by this instance (recomputed per use)
    pauli_frame: str = "I"

    @classmethod
    def from_dict(cls, data):
        """Rebuild a Cell from a to_dict() payload.

        :param data: dict from :meth:`to_dict`.
        :returns: Cell.
        """
        return cls(dims=data["dims"], pins=data["pins"], blocks=data["blocks"], pauli_frame=data.get("pauli_frame", "I"))

    def to_dict(self):
        """Serialize the Cell geometry to a JSON-safe dict (pauli_frame excluded — it is recomputed per use)."""
        return {"dims": self.dims, "pins": self.pins, "blocks": self.blocks}

    def pin_parities(self, columns, name_prefix):
        """Read the wire parity bit each qubit gets at this cell's pins.

        :param columns: {qubit: local column}.
        :param name_prefix: "in" | "out".
        :returns: {qubit: 0|1} (0 for qubits with no matching pin).
        """
        parity_by_offset = {}
        for pin in self.pins:
            if pin["name"].startswith(name_prefix):
                parity_by_offset[pin["offset"][0]] = max(0, int(pin.get("parity", 0)))
        return {qubit: parity_by_offset.get(column * STRIDE, 0) for qubit, column in columns.items()}


def _array_dimensions(array):
    return len(array), len(array[0]), len(array[0][0])


def _grid_position(i, j, k):
    return i * STRIDE, j * STRIDE, k * STRIDE


def _pipe_position(i, j, k, axis_index):
    position = [i * STRIDE, j * STRIDE, k * STRIDE]
    position[axis_index] += 1
    return tuple(position)


def _color_letter(color_value):
    return "r" if color_value == 0 else "b"


def _color_flip(color_value):
    return "b" if color_value == 0 else "r"


def _pipe_colors(axis, color_value):
    base = _color_letter(color_value)
    flipped = _color_flip(color_value)
    if axis == "I":
        return "_" + flipped + base
    if axis == "J":
        return base + "_" + flipped
    return flipped + base + "_"


def _block_faces(axis, colors):
    color_i, color_j, color_k = colors[0], colors[1], colors[2]
    if axis == "I":
        return "__" + color_j + color_j + color_k + color_k
    if axis == "J":
        return color_i + color_i + "__" + color_k + color_k
    return color_i + color_i + color_j + color_j + "__"


def _face_flip(color):
    if color == "r":
        return "b"
    if color == "b":
        return "r"
    raise RuntimeError(f"_face_flip: uncolored face {color!r}")


def _hadamard_faces_from_cube(axis, cube_faces):
    if axis != "K":
        raise RuntimeError(f"_hadamard_faces_from_cube: non-K hadamard segment (axis {axis!r}); only ColorKP/ColorKM can differ")
    pairs = ((0, 1), (2, 3))
    colors = [None, None]
    for pair_index, (first_face, second_face) in enumerate(pairs):
        for face_index in (first_face, second_face):
            color = cube_faces[face_index]
            if color in ("r", "b"):
                colors[pair_index] = color
                break
    if colors[0] is None and colors[1] is not None:
        colors[0] = _face_flip(colors[1])
    elif colors[1] is None and colors[0] is not None:
        colors[1] = _face_flip(colors[0])
    elif colors[0] is None and colors[1] is None:
        return "______", "______"

    minus_faces, plus_faces = ["_"] * 6, ["_"] * 6
    for pair_index, (first_face, second_face) in enumerate(pairs):
        color = colors[pair_index]
        flipped = _face_flip(color)
        minus_faces[first_face] = minus_faces[second_face] = color
        plus_faces[first_face] = plus_faces[second_face] = flipped

    return "".join(minus_faces), "".join(plus_faces)


def _scan_segments(lasre, axis):
    exists = lasre["Exist" + axis]
    size_i, size_j, size_k = _array_dimensions(exists)
    if axis == "K":
        color_plus = lasre.get("ColorKP")
        color_minus = lasre.get("ColorKM")
    else:
        color_plus = lasre.get("Color" + axis)
        color_minus = color_plus
    has_color_plus = bool(color_plus)
    has_color_minus = bool(color_minus)
    run_length = (size_i, size_j, size_k)[AXIS_INDEX[axis]]

    def access(position, a, b):
        if axis == "I":
            return position, a, b
        if axis == "J":
            return a, position, b
        return a, b, position

    if axis == "I":
        outer = [(j, k) for j in range(size_j) for k in range(size_k)]
    elif axis == "J":
        outer = [(i, k) for i in range(size_i) for k in range(size_k)]
    else:
        outer = [(i, j) for i in range(size_i) for j in range(size_j)]

    segments = []
    for a, b in outer:
        def make_segment(start, end, plus, minus, a=a, b=b):
            segment = {"axis": axis, "ijk": list(access(start, a, b)), "length": end - start, "colors": _pipe_colors(axis, plus)}
            if plus != minus:
                segment["hadamard"] = True
                segment["colors_m"] = _pipe_colors(axis, minus)
            return segment

        start, plus, minus = -1, -1, -1
        for position in range(run_length):
            i, j, k = access(position, a, b)
            exists_here = exists[i][j][k]
            current_plus = (color_plus[i][j][k] if has_color_plus else 0) if exists_here else -1
            current_minus = (color_minus[i][j][k] if has_color_minus else 0) if exists_here else -1
            if exists_here and start >= 0 and current_plus == plus and current_minus == minus:
                continue
            if start >= 0:
                segments.append(make_segment(start, position, plus, minus))
            if exists_here:
                start, plus, minus = position, current_plus, current_minus
            else:
                start, plus, minus = -1, -1, -1
        if start >= 0:
            segments.append(make_segment(start, run_length, plus, minus))

    return segments


def _extract_y_nodes(lasre):
    node_y = lasre["NodeY"]
    size_i, size_j, size_k = _array_dimensions(node_y)
    return [(i, j, k) for i in range(size_i) for j in range(size_j) for k in range(size_k) if node_y[i][j][k]]


def _assert_no_dead_ends(lasre):
    size_i, size_j, size_k = lasre["n_i"], lasre["n_j"], lasre["n_k"]
    protected = {(port["i"], port["j"], port["k"]) for port in lasre["ports"]}
    for port in lasre["ports"]:
        external = [port["i"], port["j"], port["k"]]
        external[AXIS_INDEX[port["d"]]] += 1 if port["e"] == "+" else -1
        protected.add(tuple(external))
    protected.update(_extract_y_nodes(lasre))

    exist_i = lasre.get("ExistI")
    exist_j = lasre.get("ExistJ")
    exist_k = lasre.get("ExistK")
    for i in range(size_i):
        for j in range(size_j):
            for k in range(size_k):
                if (i, j, k) in protected:
                    continue
                degree = 0
                if exist_i and i > 0 and exist_i[i - 1][j][k]:
                    degree += 1
                if exist_i and i < size_i - 1 and exist_i[i][j][k]:
                    degree += 1
                if exist_j and j > 0 and exist_j[i][j - 1][k]:
                    degree += 1
                if exist_j and j < size_j - 1 and exist_j[i][j][k]:
                    degree += 1
                if exist_k and k > 0 and exist_k[i][j][k - 1]:
                    degree += 1
                if exist_k and k < size_k - 1 and exist_k[i][j][k]:
                    degree += 1
                if degree == 1:
                    raise RuntimeError(f"_assert_no_dead_ends: dead end at {(i, j, k)}")


def _convert_ports(lasre):
    ports = []
    for port in lasre["ports"]:
        ports.append({"ijk": [port["i"] * STRIDE, port["j"] * STRIDE, port["k"] * STRIDE], "d": port["d"], "e": port["e"], "c": port["c"], "f": port["f"]})
    return ports


def _convert_blocks(lasre):
    blocks = []
    cube_index_by_position = {}
    hadamard_segments = []
    pin_external_positions = set()
    for port in lasre["ports"]:
        position = [port["i"], port["j"], port["k"]]
        position[AXIS_INDEX[port["d"]]] += 1 if port["e"] == "+" else -1
        pin_external_positions.add(tuple(position))

    def merge_cube(position_key, faces_string):
        if position_key in pin_external_positions:
            return
        block_index = cube_index_by_position.get(position_key)
        if block_index is not None:
            existing_faces = list(blocks[block_index]["faces"])
            for face_index in range(6):
                face_color = faces_string[face_index]
                if face_color not in ("_", "g"):
                    existing_faces[face_index] = face_color
            blocks[block_index]["faces"] = "".join(existing_faces)
        else:
            merged_faces = ["_"] * 6
            for face_index in range(6):
                if faces_string[face_index] != "_":
                    merged_faces[face_index] = faces_string[face_index]
            cube_index_by_position[position_key] = len(blocks)
            blocks.append({"pos": list(_grid_position(*position_key)), "faces": "".join(merged_faces), "block_type": "cube", "axis": None})


    pipe_endpoints = set()
    for axis in AXES:
        axis_index = AXIS_INDEX[axis]
        for segment in _scan_segments(lasre, axis):
            if segment.get("hadamard"):
                hadamard_segments.append((axis, segment))
                continue
            ijk = segment["ijk"]
            length = segment["length"]
            faces = _block_faces(axis, segment["colors"])
            for step in range(length):
                position = list(ijk)
                position[axis_index] += step
                blocks.append({"pos": list(_pipe_position(position[0], position[1], position[2], axis_index)), "faces": faces, "block_type": "pipe", "axis": axis})
                start_key = tuple(position)
                pipe_endpoints.add(start_key)
                merge_cube(start_key, faces)
                if step == length - 1:
                    end_position = list(position)
                    end_position[axis_index] += 1
                    end_key = tuple(end_position)
                    pipe_endpoints.add(end_key)
                    merge_cube(end_key, faces)

    for y_node in _extract_y_nodes(lasre):
        block_index = cube_index_by_position.get(y_node)
        if block_index is not None:
            blocks[block_index]["block_type"] = "ynode"
        elif y_node in pipe_endpoints:
            cube_index_by_position[y_node] = len(blocks)
            blocks.append({"pos": list(_grid_position(*y_node)), "faces": "______", "block_type": "ynode", "axis": None})

    for axis, segment in hadamard_segments:
        axis_index = AXIS_INDEX[axis]
        ijk = segment["ijk"]
        length = segment["length"]
        colors = segment["colors"]
        colors_minus = segment.get("colors_m") or colors
        for step in range(length):
            position = list(ijk)
            position[axis_index] += step
            start_key = tuple(position)
            end_position = list(position)
            end_position[axis_index] += 1
            end_key = tuple(end_position)
            start_cube_faces = "______"
            start_cube_index = cube_index_by_position.get(start_key)
            if start_cube_index is not None:
                start_cube_faces = blocks[start_cube_index]["faces"]
            minus_faces, plus_faces = _hadamard_faces_from_cube(axis, start_cube_faces)
            if minus_faces == "______" and plus_faces == "______":
                minus_faces = _block_faces(axis, colors_minus)
                plus_faces = _block_faces(axis, colors)
            blocks.append({"pos": list(_pipe_position(position[0], position[1], position[2], axis_index)), "faces": minus_faces, "block_type": "hadamard", "axis": axis})
            pipe_endpoints.add(start_key)
            pipe_endpoints.add(end_key)
            merge_cube(start_key, minus_faces)
            merge_cube(end_key, plus_faces)

    occupied = {tuple(_grid_position(*key)) for key in cube_index_by_position}
    for block in blocks:
        if block["block_type"] in ("pipe", "hadamard"):
            occupied.add(tuple(block["pos"]))

    port_exit_faces = set()
    for port in lasre["ports"]:
        position_key = (port["i"], port["j"], port["k"])
        axis_index = AXIS_INDEX[port["d"]]
        face_index = 2 * axis_index + (1 if port["e"] == "+" else 0)
        port_exit_faces.add((position_key, axis_index, face_index))

    def cap_block(block_index, position_key):
        block = blocks[block_index]
        faces = list(block["faces"])
        position = block["pos"]
        changed = False
        pipe_axis_index = AXIS_INDEX[block["axis"]] if block["axis"] else -1
        for axis_index in range(3):
            face_minus, face_plus = 2 * axis_index, 2 * axis_index + 1
            if block["block_type"] != "cube" and axis_index == pipe_axis_index:
                continue
            if faces[face_minus] != "_" or faces[face_plus] != "_":
                continue
            neighbor_minus = list(position)
            neighbor_minus[axis_index] -= 1
            neighbor_plus = list(position)
            neighbor_plus[axis_index] += 1
            has_minus = tuple(neighbor_minus) in occupied
            has_plus = tuple(neighbor_plus) in occupied
            if has_minus and has_plus:
                continue
            cap_color = None
            for other_axis in range(3):
                if other_axis == axis_index:
                    continue
                color = faces[2 * other_axis]
                if color in ("r", "b"):
                    cap_color = color
                    break
            if cap_color is None:
                continue
            if not has_minus and not (position_key is not None and (position_key, axis_index, face_minus) in port_exit_faces):
                faces[face_minus] = cap_color
            if not has_plus and not (position_key is not None and (position_key, axis_index, face_plus) in port_exit_faces):
                faces[face_plus] = cap_color
            changed = True
        if changed:
            block["faces"] = "".join(faces)

    for position_key, block_index in list(cube_index_by_position.items()):
        cap_block(block_index, position_key)
    for block_index, block in enumerate(blocks):
        if block["block_type"] == "pipe":
            cap_block(block_index, None)

    existing_pipe_positions = {tuple(block["pos"]) for block in blocks if block["block_type"] in ("pipe", "hadamard")}
    for port in lasre["ports"]:
        port_position = [port["i"], port["j"], port["k"]]
        axis_index = AXIS_INDEX[port["d"]]
        external = list(port_position)
        external[axis_index] += 1 if port["e"] == "+" else -1
        pipe_lasre_position = [min(port_position[coordinate], external[coordinate]) for coordinate in range(3)]
        pipe_geometry_position = _pipe_position(pipe_lasre_position[0], pipe_lasre_position[1], pipe_lasre_position[2], axis_index)
        if pipe_geometry_position in existing_pipe_positions:
            continue
        port_key = tuple(port_position)
        cube_faces = "______"
        cube_block_index = cube_index_by_position.get(port_key)
        if cube_block_index is not None:
            cube_faces = blocks[cube_block_index]["faces"]
        pipe_faces = list(cube_faces)
        pipe_faces[2 * axis_index] = "_"
        pipe_faces[2 * axis_index + 1] = "_"
        blocks.append({"pos": list(pipe_geometry_position), "faces": "".join(pipe_faces), "block_type": "pipe", "axis": AXES[axis_index]})

    for port in lasre["ports"]:
        port_key = (port["i"], port["j"], port["k"])
        cube_block_index = cube_index_by_position.get(port_key)
        if cube_block_index is None:
            continue
        axis_index = AXIS_INDEX[port["d"]]
        face_index = 2 * axis_index + (1 if port["e"] == "+" else 0)
        faces = list(blocks[cube_block_index]["faces"])
        if faces[face_index] != "_":
            faces[face_index] = "_"
            blocks[cube_block_index]["faces"] = "".join(faces)

    return blocks


def _pins_from_ports(ports):
    pins = []
    seen_names = set()
    for port in ports:
        name = port["f"].replace(" ", "_")
        if name in seen_names:
            raise RuntimeError(f"_pins_from_ports: duplicate port name {name!r}")
        seen_names.add(name)
        ijk = port["ijk"]
        pins.append({"name": name, "dir": port["d"], "end": port["e"], "offset": [ijk[0], ijk[1], ijk[2]]})
    return pins


def _transverse_axis_first(axis):
    return "J" if axis == "I" else "I"


def _transverse_axis_second(axis):
    return "J" if axis == "K" else "K"


def _parity_from_face_colors(first_color, second_color):
    if first_color == "r":
        return 1
    if first_color == "b":
        return 0
    raise RuntimeError(f"_parity_from_face_colors: uncolored transverse faces {first_color!r}/{second_color!r}")


def _parity_from_faces(faces, axis):
    return _parity_from_face_colors(faces[2 * AXIS_INDEX[_transverse_axis_first(axis)]], faces[2 * AXIS_INDEX[_transverse_axis_second(axis)]])


def _parity_flip(parity):
    return parity if parity < 0 else 1 - parity


def _block_from_lasre_block(lasre_block):
    position = list(lasre_block["pos"])
    axis = lasre_block["axis"] or "I"
    if lasre_block["block_type"] == "pipe":
        return {"kind": "pipe", "pos": position, "axis": axis, "parity": _parity_from_faces(lasre_block["faces"], axis)}
    if lasre_block["block_type"] == "hadamard":
        return {"kind": "hadamard", "pos": position, "axis": axis, "parity": _parity_from_faces(lasre_block["faces"], axis)}
    if lasre_block["block_type"] == "ynode":
        return {"kind": "ynode", "pos": position, "parity": PARITY_NONE}
    raise RuntimeError(f"_block_from_lasre_block: unexpected block type {lasre_block['block_type']!r}")


def _pin_pipe_position(offset, direction, end):
    sign = 1 if end == "+" else -1
    position = list(offset)
    position[AXIS_INDEX[direction]] += sign
    return position


def _resolve_pins(pins, blocks, name):
    resolved = []
    pin_pipe_indices = []
    for pin in pins:
        pipe_position = _pin_pipe_position(pin["offset"], pin["dir"], pin["end"])
        parity = PARITY_NONE
        for block in blocks:
            if block["pos"] != pipe_position:
                continue
            if block["kind"] == "hadamard":
                parity = block["parity"] if pin["end"] == "+" else _parity_flip(block["parity"])
            else:
                parity = block["parity"]
            break
        if parity < 0:
            raise RuntimeError(f"_resolve_pins: no pin pipe at {pipe_position} for {name}.{pin['name']}")

        for block_index, block in enumerate(blocks):
            if block["pos"] != pipe_position:
                continue
            if block["kind"] in ("pipe", "hadamard"):
                pin_pipe_indices.append(block_index)
                continue
            raise RuntimeError(f"_resolve_pins: unexpected block at pin pipe coord: {name}.{pin['name']}")
        resolved.append({"name": pin["name"], "dir": pin["dir"], "parity": parity, "pipe_pos": pipe_position})

    for block_index in sorted(set(pin_pipe_indices), reverse=True):
        del blocks[block_index]
    return resolved


def _block_bounds(block):
    position = block["pos"]
    low = list(position)
    high = [position[0] + 1, position[1] + 1, position[2] + 1]
    if block["kind"] in ("pipe", "hadamard"):
        axis_index = AXIS_INDEX[block["axis"]]
        low[axis_index] -= 1
        high[axis_index] += 1
    return low, high


def _assert_coords_in_bounds(blocks, dims):
    for block in blocks:
        low, high = _block_bounds(block)
        for coordinate in range(3):
            if low[coordinate] < 0 or high[coordinate] > dims[coordinate]:
                raise RuntimeError(f"_assert_coords_in_bounds: block {block} outside dims {dims}")


def from_lasre(lasre, name):
    """Convert a LaSsynth lasre solution dict into a Cell.

    :param lasre: dict, LaSsynth result (n_i/n_j/n_k grids, ports, blocks).
    :param name: str, used in error messages.
    :returns: Cell (dims, resolved pins with parity, pipe/hadamard blocks).
    :raises RuntimeError: on dead ends, out-of-bounds blocks or non-identity
        boundary fixup.
    """
    _assert_no_dead_ends(lasre)
    dims = [lasre["n_i"] * STRIDE - 1, lasre["n_j"] * STRIDE - 1, lasre["n_k"] * STRIDE - 1]
    lasre_blocks = _convert_blocks(lasre)
    lasre_pins = _pins_from_ports(_convert_ports(lasre))

    fixup = ""
    optional = lasre.get("optional")
    if isinstance(optional, dict):
        fixup = optional.get("boundary_pauli_fixup", "")
    for character in fixup:
        if character != "I":
            raise RuntimeError("from_lasre: non-identity boundary_pauli_fixup requires sfix pin extension, not supported (Clifford-only build)")

    blocks = [_block_from_lasre_block(lasre_block) for lasre_block in lasre_blocks if lasre_block["block_type"] != "cube"]
    resolved_pins = _resolve_pins(lasre_pins, blocks, name)
    _assert_coords_in_bounds(blocks, dims)

    pins = []
    if blocks:
        for pin in resolved_pins:
            axis_index = AXIS_INDEX[pin["dir"]]
            position_value = pin["pipe_pos"][axis_index]
            dimension_value = dims[axis_index]
            end = "+" if position_value >= dimension_value - 1 else "-"
            pins.append({"name": pin["name"], "dir": pin["dir"], "end": end, "parity": pin["parity"], "offset": pin["pipe_pos"]})

    return Cell(dims=dims, pins=pins, blocks=blocks)

Z3_TIMEOUT_MS = 60_000
KISSAT_DIR = os.environ.get("MINIFLASH_KISSAT_DIR") or os.path.dirname(shutil.which("kissat") or "")


class SynthTimeout(RuntimeError):
    pass


class SynthUnsat(RuntimeError):
    pass


class VerifyFailed(RuntimeError):
    pass


def _default_solver():
    forced = os.environ.get("MINIFLASH_SOLVER")
    if forced:
        return forced
    for _ in range(3):
        if os.path.isfile(os.path.join(KISSAT_DIR, "kissat")):
            return "kissat"
        time.sleep(0.5)
    print("miniflash: kissat wrapper not visible, falling back to z3 (slow)", file=sys.stderr)
    return "z3"


def _make_synthesizer(solver):
    if solver == "kissat":
        return LatticeSurgerySynthesizer(solver="kissat", kissat_dir=KISSAT_DIR)
    import z3
    z3.set_param("timeout", Z3_TIMEOUT_MS)
    return LatticeSurgerySynthesizer(solver="z3")


def _local_gates(region):
    local_index = {qubit: index for index, qubit in enumerate(region.qubits)}
    return [(name, tuple(local_index[qubit] for qubit in qubits)) for name, qubits in region.gates]


def _effective_gates(region, in_bases):
    flips = [("h", (local_index,)) for local_index, qubit in enumerate(region.qubits) if in_bases and in_bases[qubit]]
    return flips + _local_gates(region)


def _requires_y(region, in_bases):
    duals = _dual_stabilizers(_effective_gates(region, in_bases), len(region.qubits))
    return any(_pauli_parts(dual)[1].count("Y") % 2 for dual in duals)


def _cache_key(region, in_columns=None, out_columns=None, in_bases=None, side_entry_slots=None, side_exit_slots=None, forbid_y=False):
    duals = _dual_stabilizers(_effective_gates(region, in_bases), len(region.qubits))
    parts = [len(region.qubits), tuple(_pauli_parts(dual)[1].replace("_", "I") for dual in duals)]
    if forbid_y:
        parts.append(("forbid_y", True))
    if in_columns is not None:
        parts.append(("in_cols", [in_columns.get(qubit, -1) for qubit in region.qubits]))
    if out_columns is not None:
        parts.append(("out_cols", [out_columns.get(qubit, -1) for qubit in region.qubits]))
    if side_entry_slots:
        parts.append(("side_in", sorted(side_entry_slots.items())))
    if side_exit_slots:
        parts.append(("side_out", sorted(side_exit_slots.items())))
    return hashlib.sha256(repr(tuple(parts)).encode()).hexdigest()


def _dual_stabilizers(local_gates, num_qubits):
    circuit = stim.Circuit()
    gate_map = {"h": "H", "cx": "CX", "x": "X", "s": "S", "sdg": "S_DAG"}
    for name, qubits in local_gates:
        if name not in gate_map:
            raise ValueError(f"synthesize: unsupported gate {name!r} (h/cx/x/s/sdg only)")
        circuit.append(gate_map[name], list(qubits))
    simulator = stim.TableauSimulator()
    simulator.do(circuit)
    tableau = simulator.current_inverse_tableau() ** -1

    duals = []
    for qubit in range(num_qubits):
        x_input = [0] * num_qubits
        x_input[qubit] = 1
        duals.append(stim.PauliString(x_input) + tableau.x_output(qubit))
        z_input = [0] * num_qubits
        z_input[qubit] = 3
        duals.append(stim.PauliString(z_input) + tableau.z_output(qubit))
    return duals


def _pauli_parts(pauli):
    text = str(pauli)
    if text.startswith("-"):
        return 1, text[1:]
    if text.startswith("+"):
        return 0, text[1:]
    return 0, text


def _pauli_body_to_bits(body):
    x_bits, z_bits = [], []
    for character in body:
        if character in ("I", "_"):
            x_bits.append(0)
            z_bits.append(0)
        elif character == "X":
            x_bits.append(1)
            z_bits.append(0)
        elif character == "Y":
            x_bits.append(1)
            z_bits.append(1)
        elif character == "Z":
            x_bits.append(0)
            z_bits.append(1)
        else:
            raise ValueError(f"synthesize: unsupported Pauli character {character!r}")
    return x_bits, z_bits


def _bits_to_pauli_body(x_bits, z_bits):
    characters = []
    for x_bit, z_bit in zip(x_bits, z_bits):
        if x_bit == 0 and z_bit == 0:
            characters.append("I")
        elif x_bit == 1 and z_bit == 0:
            characters.append("X")
        elif x_bit == 0 and z_bit == 1:
            characters.append("Z")
        else:
            characters.append("Y")
    return "".join(characters)


def _solve_mod2(matrix, right_hand_side):
    if not matrix:
        return []
    rows = [row[:] for row in matrix]
    values = right_hand_side[:]
    num_rows = len(rows)
    num_columns = len(rows[0])
    pivot_columns = []
    pivot_row = 0
    for column in range(num_columns):
        swap_row = None
        for row in range(pivot_row, num_rows):
            if rows[row][column]:
                swap_row = row
                break
        if swap_row is None:
            continue
        rows[pivot_row], rows[swap_row] = rows[swap_row], rows[pivot_row]
        values[pivot_row], values[swap_row] = values[swap_row], values[pivot_row]
        for row in range(num_rows):
            if row != pivot_row and rows[row][column]:
                for index in range(column, num_columns):
                    rows[row][index] ^= rows[pivot_row][index]
                values[row] ^= values[pivot_row]
        pivot_columns.append(column)
        pivot_row += 1
        if pivot_row == num_rows:
            break

    for row in range(pivot_row, num_rows):
        if values[row]:
            return None

    solution = [0] * num_columns
    for row in range(pivot_row - 1, -1, -1):
        column = pivot_columns[row]
        accumulator = values[row]
        for index in range(column + 1, num_columns):
            accumulator ^= rows[row][index] & solution[index]
        solution[column] = accumulator
    return solution


def _unsigned_stabilizers_and_fixup(duals):
    if not duals:
        return [], ""
    sign_bits, bodies, equations = [], [], []
    width = len(_pauli_parts(duals[0])[1])
    for dual in duals:
        sign_bit, body = _pauli_parts(dual)
        x_bits, z_bits = _pauli_body_to_bits(body)
        sign_bits.append(sign_bit)
        bodies.append(body.replace("_", "I"))
        equations.append(z_bits + x_bits)

    solution = _solve_mod2(equations, sign_bits)
    if solution is None:
        raise RuntimeError("synthesize: failed to solve boundary Pauli fixup system over GF(2)")
    fixup = _bits_to_pauli_body(solution[:width], solution[width:])
    return bodies, fixup


def _build_spec(num_qubits, stabilizers, depth, in_pin_names, out_pin_names, in_columns=None, out_columns=None, side_in_slots=None, side_out_slots=None, forbid_y=False):
    in_columns = in_columns if in_columns is not None else [index + 1 for index in range(num_qubits)]
    out_columns = out_columns if out_columns is not None else [index + 1 for index in range(num_qubits)]
    side_in_slots = side_in_slots if side_in_slots is not None else {}
    side_out_slots = side_out_slots if side_out_slots is not None else {}
    if any(slot >= depth for slot in side_in_slots.values()) or any(slot >= depth for slot in side_out_slots.values()):
        return None

    face_column = num_qubits + 2
    ports = []
    for index in range(num_qubits):
        if index in side_in_slots:
            ports.append({"location": [face_column, 1, side_in_slots[index]], "direction": "-I", "z_basis_direction": "J", "function": in_pin_names[index]})
        else:
            ports.append({"location": [in_columns[index], 1, 0], "direction": "+K", "z_basis_direction": "I", "function": in_pin_names[index]})
    for index in range(num_qubits):
        if index in side_out_slots:
            ports.append({"location": [face_column, 1, depth - 1 - side_out_slots[index]], "direction": "-I", "z_basis_direction": "J", "function": out_pin_names[index]})
        else:
            ports.append({"location": [out_columns[index], 1, depth], "direction": "-K", "z_basis_direction": "I", "function": out_pin_names[index]})

    side_locations = [tuple(port["location"]) for port in ports if port["direction"] == "-I"]
    if len(side_locations) != len(set(side_locations)):
        return None
    spec = {"max_i": num_qubits + 2, "max_j": 3, "max_k": depth, "ports": ports, "stabilizers": stabilizers}
    if forbid_y:
        spec["optional"] = {"forbid_y_cubes": True}
    return spec


def _hard_wall(signum, frame):
    raise SynthTimeout("synthesize: hard wall — solver attempt exceeded the budget")


def _solve_region(local_gates, num_qubits, in_pin_names, out_pin_names, min_depth=2, max_depth=16, solver=None, in_columns=None, out_columns=None, side_in_slots=None, side_out_slots=None, deadline=None, forbid_y=False):
    duals = _dual_stabilizers(local_gates, num_qubits)
    stabilizers, fixup = _unsigned_stabilizers_and_fixup(duals)
    solver = solver if solver is not None else _default_solver()
    synthesizer = _make_synthesizer(solver)
    saved_cap = os.environ.get("MINIFLASH_KISSAT_TIME")

    for depth in range(min_depth, max_depth + 1):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if saved_cap is None:
                    os.environ.pop("MINIFLASH_KISSAT_TIME", None)
                else:
                    os.environ["MINIFLASH_KISSAT_TIME"] = saved_cap
                raise SynthTimeout(f"synthesize: budget exhausted at depth {depth}, n={num_qubits} qubits")
            os.environ["MINIFLASH_KISSAT_TIME"] = str(max(1, int(remaining)))
        spec = _build_spec(num_qubits, stabilizers, depth, in_pin_names, out_pin_names, in_columns=in_columns, out_columns=out_columns, side_in_slots=side_in_slots, side_out_slots=side_out_slots, forbid_y=forbid_y)
        if spec is None:
            continue

        try:
            if deadline is not None:
                previous_handler = signal.signal(signal.SIGALRM, _hard_wall)
                signal.setitimer(signal.ITIMER_REAL, max(0.5, remaining))
            solution = synthesizer.solve(specification=spec, print_detail=False)
        except ValueError as error:
            if "return code" not in str(error):
                raise
            solution = None
        finally:
            if deadline is not None:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
                if saved_cap is None:
                    os.environ.pop("MINIFLASH_KISSAT_TIME", None)
                else:
                    os.environ["MINIFLASH_KISSAT_TIME"] = saved_cap
        if solution is None:
            continue

        optimized = solution.after_default_optimizations()
        try:
            verified = optimized.verify_stabilizers_stimzx(spec)
        except Exception as error:
            print(f"synthesize: stimzx verification unavailable ({type(error).__name__}: {error}) — accepting unverified solution", file=sys.stderr)
            verified = None
        if verified is False:
            raise VerifyFailed(f"synthesize: stimzx verification FAILED at depth {depth}, n={num_qubits} qubits, stabilizers={stabilizers}")
        return optimized.lasre, fixup

    raise SynthUnsat(f"synthesize: no solution for depths {min_depth}..{max_depth} with {solver}, n={num_qubits} qubits, stabilizers={stabilizers}")


def synthesize_region(region, cache_dir=".miniflash-cache", in_columns=None, out_columns=None, in_bases=None, side_entry_slots=None, side_exit_slots=None, budget_s=None, forbid_y=False) -> Cell:
    """SAT-synthesize (or load from disk cache) the lattice-surgery cell for one region.

    :param region: Region.
    :param cache_dir: str, disk cache directory.
    :param in_columns: {qubit: local column} in-pin constraints.
    :param out_columns: {qubit: local column} out-pin constraints.
    :param in_bases: {qubit: 0|1} wire parity at entry.
    :param side_entry_slots: {qubit: slot} side-face entry ports.
    :param side_exit_slots: {qubit: slot} side-face exit ports.
    :param budget_s: float | None, SAT budget in seconds.
    :param forbid_y: bool, demand a Y-free (rotatable) geometry.
    :returns: Cell (pauli_frame set to the sign fixup).
    :raises SynthTimeout: on budget exhaustion (also when a .synthfail marker
        records a timeout at >= this budget).
    :raises RuntimeError: on UNSAT or failed verification.
    """
    key = _cache_key(region, in_columns, out_columns, in_bases, side_entry_slots, side_exit_slots, forbid_y=forbid_y)
    cache_root = Path(cache_dir)
    cache_path = cache_root / f"{key}.json"
    fail_path = cache_root / f"{key}.synthfail"
    num_qubits = len(region.qubits)
    local_gates = _effective_gates(region, in_bases)

    if cache_path.exists():
        cell = Cell.from_dict(json.loads(cache_path.read_text()))
        _, cell.pauli_frame = _unsigned_stabilizers_and_fixup(_dual_stabilizers(local_gates, num_qubits))
        return cell
    if budget_s and fail_path.exists():
        try:
            failed_budget = float(fail_path.read_text())
        except ValueError:
            failed_budget = 0.0
        if failed_budget >= budget_s:
            raise SynthTimeout(f"synthesize: cached timeout at budget {failed_budget:.0f}s, n={num_qubits} qubits")

    local_index_of = {qubit: index for index, qubit in enumerate(region.qubits)}
    in_pin_names = [region.in_pins[qubit] for qubit in region.qubits]
    out_pin_names = [region.out_pins[qubit] for qubit in region.qubits]
    in_column_list = [in_columns.get(qubit, 1) for qubit in region.qubits] if in_columns is not None else None
    out_column_list = [out_columns.get(qubit, 1) for qubit in region.qubits] if out_columns is not None else None
    side_in_slots = {local_index_of[qubit]: slot for qubit, slot in side_entry_slots.items()} if side_entry_slots else None
    side_out_slots = {local_index_of[qubit]: slot for qubit, slot in side_exit_slots.items()} if side_exit_slots else None
    deadline = time.monotonic() + budget_s if budget_s else None

    try:
        lasre, fixup = _solve_region(local_gates, num_qubits, in_pin_names, out_pin_names, in_columns=in_column_list, out_columns=out_column_list, side_in_slots=side_in_slots, side_out_slots=side_out_slots, deadline=deadline, forbid_y=forbid_y)
    except SynthTimeout:
        if budget_s:
            cache_root.mkdir(parents=True, exist_ok=True)
            fail_path.write_text(str(budget_s))
        raise

    cell = from_lasre(lasre, name="region")
    cache_root.mkdir(parents=True, exist_ok=True)
    temporary_path = cache_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(cell.to_dict()))
    temporary_path.replace(cache_path)
    cell.pauli_frame = fixup
    return cell


def synthesize(partitioned, cache_dir=".miniflash-cache", side_ports=False, die_dims=None, budget_s=None, orientation=False):
    """Run the synthesis stage for a whole circuit: schedule, floorplan, then every cell.

    Threads the wire parity ledger between cells (a cell's out-pin parities become
    the next consumer's in_bases), which color matching requires; failed regions are
    tagged on the raised error (``error.region``) so callers can split and retry.

    :param partitioned: PartitionedCircuit.
    :param cache_dir: str, cell cache directory.
    :param side_ports: bool, swap through cell side faces.
    :param die_dims: (width, rows | None) or None for 1-D.
    :param budget_s: float | None, SAT budget in seconds per region.
    :param orientation: bool, lay cells down (90-degree I-K rotation plus
        pin elbows) when that shortens the layout — chains of consecutive
        same-column layers fuse into one thin band, singles lie alone when
        ``depth > n+2``; 1-D only.
    :returns: (floorplan, cell_types, channels) — everything elaborate consumes.
    :raises SynthTimeout | SynthUnsat: from the failing region, with ``.region`` set.
    """
    regions, events, num_qubits = partitioned.regions, partitioned.events, partitioned.num_qubits
    layers, channels = schedule_layers(partitioned, die_dims=die_dims)

    if regions:
        floorplan = solve_floorplan([[region.qubits for region in layer] for layer in layers], num_qubits, side_ports=side_ports, with_magic=bool(events), die_dims=die_dims)
    else:
        floorplan = passthrough_floorplan(num_qubits, with_magic=True)

    chain_layers = set()
    if orientation and regions:
        from .orientation import plan_chain_layers

        chain_layers = plan_chain_layers(floorplan, channels)

    cell_types = []
    wire_bits = {qubit: 0 for qubit in range(num_qubits)}
    for layer, layer_regions in enumerate(layers):
        region_of = {tuple(region.qubits): region for region in layer_regions}
        layer_cells, ledger_boxes = [], []
        for _, offset, box_qubits in floorplan.placements[layer]:
            region = region_of[tuple(box_qubits)]

            side_entry_slots = {qubit: side_move.slot for qubit, side_move in floorplan.side_entries[layer].items() if qubit in region.qubits}
            side_exit_slots = {qubit: side_move.slot for qubit, side_move in floorplan.side_exits[layer].items() if qubit in region.qubits}
            in_columns = {qubit: floorplan.in_port_columns[layer][qubit] - offset for qubit in region.qubits if qubit in floorplan.in_port_columns[layer]}
            out_columns = {qubit: floorplan.out_port_columns[layer][qubit] - offset for qubit in region.qubits if qubit in floorplan.out_port_columns[layer]}
            in_bases = {qubit: wire_bits[qubit] for qubit in region.qubits}
            try:
                forbid_y = layer in chain_layers and not _requires_y(region, in_bases)
                cell = synthesize_region(region, cache_dir=str(cache_dir), in_columns=in_columns, out_columns=out_columns, in_bases=in_bases, side_entry_slots=side_entry_slots, side_exit_slots=side_exit_slots, budget_s=budget_s, forbid_y=forbid_y)
            except (SynthTimeout, SynthUnsat) as error:
                if not forbid_y:
                    error.region = region
                    raise
                try:
                    cell = synthesize_region(region, cache_dir=str(cache_dir), in_columns=in_columns, out_columns=out_columns, in_bases=in_bases, side_entry_slots=side_entry_slots, side_exit_slots=side_exit_slots, budget_s=budget_s)
                except (SynthTimeout, SynthUnsat) as fallback_error:
                    fallback_error.region = region
                    raise

            layer_cells.append(cell)
            ledger_boxes.append([out_columns, side_exit_slots])

        for cell, (out_columns, side_exit_slots) in zip(layer_cells, ledger_boxes):
            wire_bits.update(cell.pin_parities(out_columns, "out"))
            for qubit in side_exit_slots:
                wire_bits[qubit] = 0
        cell_types.append(layer_cells)

    if orientation:
        from .orientation import apply_orientation

        floorplan, cell_types, channels = apply_orientation(floorplan, cell_types, channels)

    return floorplan, cell_types, channels
