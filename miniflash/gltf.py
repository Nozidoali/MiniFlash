"""The glTF backend: a tile Program to a self-contained scene file.

:func:`write_gltf` lowers the step graphs of a :class:`~miniflash.program.Program`
to pipes on a 3-D lattice and emits glTF 2.0. Every step spans two K layers,
2t and 2t + 1; tile (row, col) on layer k sits at cube
(STRIDE * col, STRIDE * row, STRIDE * k).

* each qubit is a red K (time) pipe through every layer; a walk adds its I/J
  pipes (orange) on the first layer of the later step;
* a CX merge runs from the Z side of its control to the X side of its target.
  Every qubit faces the same way, so the route leaves the control vertically and
  enters the target horizontally and turns somewhere; its first turn is the
  ancilla. The Z half, from the control to the ancilla, lies on the lower layer
  (blue); a K pipe climbs at the ancilla (blue); the X half, from the ancilla to
  the target, lies on the upper layer (red);
* a T merge lies on the lower layer (blue), the magic state prepared in whatever
  basis its side needs;
* grey cubes mark every occupied tile, green boxes the consumed magic tiles;
* with a :class:`~miniflash.supply.Supply`, each factory is a green box through the
  whole program outside the delivery rings, and each consumed state a green path
  from its factory's port to its magic tile on the layer where the T merge runs.
"""
import base64
import json
import struct
from typing import NamedTuple

from .program import MAGIC, QUBIT, ROUTE

STRIDE = 2

_UNIT_POSITIONS = ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))
_UNIT_INDICES = (0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 0, 5, 1, 0, 4, 5, 3, 2, 6, 3, 6, 7, 0, 3, 7, 0, 7, 4, 1, 5, 6, 1, 6, 2)


PIPE_AXIS_SCALE = 1.4
PIPE_CROSS_SCALE = 0.55


class _Pipe(NamedTuple):
    #: low cube endpoint
    lo: tuple
    #: high cube endpoint
    hi: tuple
    #: "I" | "J" | "K"
    axis: str
    #: color bit for rendering: 0 red (X), 1 blue (Z)
    parity: int
    #: Hadamard wall on this segment
    hadamard: bool = False
    #: magic-state volume
    t_volume: bool = False
    #: a qubit moving between partitions
    walk: bool = False


def _cube(tile, layer):
    return (STRIDE * tile[1], STRIDE * tile[0], STRIDE * layer)


def _pipe(a, b, parity):
    """Pipe between two cubes that differ on exactly one axis."""
    lo, hi = min(a, b), max(a, b)
    axis = "IJK"[next(i for i in range(3) if lo[i] != hi[i])]
    return _Pipe(lo, hi, axis, parity)


def _route_path(step, comp, z_tile):
    """Tiles of a merge component in walking order from the Z party to the X party."""
    adj = step.neighbours()
    path, prev = [z_tile], None
    while True:
        nxt = [n for n in adj[path[-1]] if n != prev and n in comp]
        if not nxt or (len(path) > 1 and step.vertices[path[-1]].kind != ROUTE):
            return path
        prev = path[-1]
        path.append(nxt[0])


def _z_party(step, comp):
    """The qubit merged through its top or bottom: the control of a CX, the qubit of a T."""
    adj = step.neighbours()
    for tile in comp:
        if step.vertices[tile].kind == QUBIT and all(n[1] == tile[1] for n in adj[tile]):
            return tile
    raise ValueError(f"merge {sorted(comp)} enters no qubit through its Z side")


def _direction(a, b):
    return (b[0] - a[0], b[1] - a[1])


def ancilla(path):
    """Index in `path` of the first tile where the route turns."""
    for i in range(1, len(path) - 1):
        if _direction(path[i - 1], path[i]) != _direction(path[i], path[i + 1]):
            return i
    raise ValueError(f"CX route {path} runs along one line: no turn for the ancilla")


def _merge_pipes(step, t):
    """The two layers of step t: Z halves and T merges below, the ancilla K pipes,
    X halves above."""
    lower_layer, upper_layer = 2 * t, 2 * t + 1
    pipes = []
    for comp in step.components():
        path = _route_path(step, comp, _z_party(step, comp))
        if step.vertices[path[-1]].kind == MAGIC:
            pipes.extend(_pipe(_cube(a, lower_layer), _cube(b, lower_layer), 1) for a, b in zip(path, path[1:]))
            continue
        k = ancilla(path)
        pipes.extend(_pipe(_cube(a, lower_layer), _cube(b, lower_layer), 1) for a, b in zip(path[:k], path[1:k + 1]))
        pipes.append(_pipe(_cube(path[k], lower_layer), _cube(path[k], upper_layer), 1))
        pipes.extend(_pipe(_cube(a, upper_layer), _cube(b, upper_layer), 0) for a, b in zip(path[k:], path[k + 1:]))
    return pipes


def _qubit_pipes(step, t, after=None):
    """Each qubit's K pipe through step t and, when a later step follows, on to it,
    with the walk's I/J pipes on that step's first layer."""
    pipes = []
    here = step.qubit_tiles()
    there = after.qubit_tiles() if after is not None else {}
    for q, src in here.items():
        pipes.append(_pipe(_cube(src, 2 * t), _cube(src, 2 * t + 1), 0))
        if after is None:
            continue
        dst, nxt = there[q], 2 * t + 2
        pipes.append(_pipe(_cube(src, 2 * t + 1), _cube(src, nxt), 0))
        corner = (src[0], dst[1])
        if corner != src:
            pipes.append(_pipe(_cube(src, nxt), _cube(corner, nxt), 0)._replace(walk=True))
        if dst != corner:
            pipes.append(_pipe(_cube(corner, nxt), _cube(dst, nxt), 0)._replace(walk=True))
    return pipes


def _delivery_pipes(supply):
    """Green pipes along every delivery path, on the lower layer of its step."""
    pipes = []
    for d in supply.deliveries:
        layer = 2 * d.step
        pipes.extend(_pipe(_cube(a, layer), _cube(b, layer), 1)._replace(t_volume=True) for a, b in zip(d.tiles, d.tiles[1:]))
    return pipes


def factory_boxes(program, supply):
    """(lo, hi) corners of each factory box, through every layer of the program."""
    top = STRIDE * (2 * len(program.steps) - 1) + 1
    return [((STRIDE * b.col, STRIDE * b.row, 0), (STRIDE * (b.col + b.width - 1) + 1, STRIDE * (b.row + b.height - 1) + 1, top))
            for b in supply.boxes]


def lower(program, supply=None):
    """Pipes, cubes and magic boxes of a Program.

    :param program: Program.
    :param supply: Supply | None, see :func:`miniflash.supply.plan`; adds the
        delivery pipes (factory ports excluded from the cubes, they sit in the boxes).
    :returns: (pipes, cubes, magic) — list of _Pipe, sorted cube positions, sorted
        consumed-magic cube positions.
    """
    pipes, cubes, magic = [], set(), set()
    steps = program.steps
    for t, step in enumerate(steps):
        for v in step.of_kind(MAGIC):
            if v.id >= 0:
                magic.add(_cube(v.tile, 2 * t))
        pipes.extend(_merge_pipes(step, t))
        pipes.extend(_qubit_pipes(step, t, steps[t + 1] if t + 1 < len(steps) else None))
    if supply is not None:
        pipes.extend(_delivery_pipes(supply))
    for pipe in pipes:
        cubes.add(pipe.lo)
        cubes.add(pipe.hi)
    cubes -= magic
    if supply is not None:
        cubes -= {_cube(b.port, 2 * d.step) for d in supply.deliveries for b in [supply.boxes[d.factory]]}
    pipes.sort(key=lambda pipe: (pipe.lo, pipe.hi, pipe.axis))
    return pipes, sorted(cubes), sorted(magic)


def _collect_pipe_boxes(pipes):
    entries = []
    for pipe in pipes:
        low, high = tuple(pipe.lo), tuple(pipe.hi)
        middle = tuple((low[coordinate] + high[coordinate]) // 2 for coordinate in range(3))
        if pipe.walk:
            material = 5
        elif pipe.hadamard:
            material = 3
        elif pipe.t_volume:
            material = 4
        else:
            material = 1 if pipe.parity >= 1 else 0
        entries.append((low, material, middle, pipe.axis))
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


def _pack_shared_mesh():
    position_bytes = struct.pack("<24f", *(value for vertex in _UNIT_POSITIONS for value in vertex))
    index_bytes = struct.pack("<36H", *_UNIT_INDICES)
    buffer = bytearray(position_bytes)
    index_offset = len(buffer)
    buffer += index_bytes
    while len(buffer) % 4 != 0:
        buffer.append(0)
    return bytes(buffer), len(position_bytes), index_offset, len(index_bytes)


def write_gltf(program, path, supply=None):
    """Render a Program as a glTF 2.0 scene file.

    Materials: red/blue parity pipes, grey cubes, yellow Hadamard, green magic, orange walks.

    :param program: Program.
    :param path: str | Path of the output ``.gltf``.
    :param supply: Supply | None; draws factories and deliveries.
    :returns: None.
    """
    pipes, cubes, magic = lower(program, supply)
    boxes = factory_boxes(program, supply) if supply is not None else []
    pipe_boxes = _collect_pipe_boxes(pipes)
    buffer, position_bytes_length, index_offset, index_bytes_length = _pack_shared_mesh()

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
            {"pbrMetallicRoughness": {"baseColorFactor": [0.15, 0.75, 0.25, 1.0]}},
            {"pbrMetallicRoughness": {"baseColorFactor": [1.00, 0.50, 0.05, 1.0]}},
        ],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": material}]} for material in range(6)],
    }

    nodes = []
    for middle, material, axis in pipe_boxes:
        nodes.append(_pipe_node(middle, material, axis))
    for position in cubes:
        nodes.append({"mesh": 2, "translation": [float(coordinate) for coordinate in position], "scale": [1.0, 1.0, 1.0]})
    for position in magic:
        nodes.append({"mesh": 4, "translation": [float(coordinate) for coordinate in position], "scale": [1.0, 1.0, 1.0]})
    for lo, hi in boxes:
        nodes.append({"mesh": 4, "translation": [float(x) for x in lo], "scale": [float(h - l) for l, h in zip(lo, hi)]})

    # root node rotates -90 deg about X so K (time) renders upward (+Y),
    # matching the lattice-surgery convention; J recedes into the screen.
    nodes.append({"children": list(range(len(nodes))), "rotation": [-0.7071068, 0.0, 0.0, 0.7071068]})
    root["nodes"] = nodes
    root["scenes"] = [{"nodes": [len(nodes) - 1]}]
    root["scene"] = 0

    with open(path, "w") as file:
        json.dump(root, file, indent=2)
        file.write("\n")
