"""The glTF backend: materialized pipes to a self-contained scene file.

:func:`write_gltf` re-runs the lowering from :mod:`miniflash.lower` with
real pipes and emits glTF 2.0 — red/blue parity pipes, grey junction
cubes, yellow Hadamard slabs, green magic-state volume.
"""
import base64
import json
import struct

from .lower import _Extent, _layout, _pipes_of, _plan
from .synthesis import STRIDE

_UNIT_POSITIONS = ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))
_UNIT_INDICES = (0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 0, 5, 1, 0, 4, 5, 3, 2, 6, 3, 6, 7, 0, 3, 7, 0, 7, 4, 1, 5, 6, 1, 6, 2)


PIPE_AXIS_SCALE = 1.4
PIPE_CROSS_SCALE = 0.55


def _collect_pipe_boxes(pipes):
    entries = []
    for pipe in pipes:
        low, high = tuple(pipe.lo), tuple(pipe.hi)
        middle = tuple((low[coordinate] + high[coordinate]) // 2 for coordinate in range(3))
        if pipe.hadamard:
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


def _collect_cubes(pipes):
    positions = set()
    for pipe in pipes:
        positions.add(tuple(pipe.lo))
        positions.add(tuple(pipe.hi))
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


def write_gltf(program, path):
    """Render a Program as a glTF 2.0 scene file.

    Re-runs the lowering with materialized pipes, then writes JSON glTF
    (materials: red/blue parity, grey cubes, yellow hadamard, green
    T-volume/factory).

    :param program: Program.
    :param path: str | Path of the output ``.gltf``.
    :returns: None.
    """
    plan = _plan(program)
    pipes = sorted(_pipes_of(program, plan), key=lambda pipe: (pipe.lo, pipe.hi, pipe.axis))
    extent = _Extent()
    for pipe in pipes:
        extent.add(pipe)
    layout = _layout(program, plan, extent)
    pipe_boxes = _collect_pipe_boxes(pipes)
    cubes = _collect_cubes(pipes)
    buffer, position_bytes_length, index_offset, index_bytes_length = _pack_shared_mesh()
    has_t_volume = any(pipe.t_volume for pipe in pipes) or bool(layout.get("factory_boxes"))

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

    # root node rotates -90 deg about X so K (time) renders upward (+Y),
    # matching the lattice-surgery convention; J recedes into the screen.
    nodes.append({"children": list(range(len(nodes))), "rotation": [-0.7071068, 0.0, 0.0, 0.7071068]})
    root["nodes"] = nodes
    root["scenes"] = [{"nodes": [len(nodes) - 1]}]
    root["scene"] = 0

    with open(path, "w") as file:
        json.dump(root, file, indent=2)
        file.write("\n")
