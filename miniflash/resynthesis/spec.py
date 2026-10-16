"""Spec: a closed window as a lassynth problem.

The window's box becomes the lassynth volume, one cube per tile and layer:
columns run along I, rows along J, layers along K. Layer 0 holds nothing but
the open ends of the input ports, so a window of L layers asks for max_k = L + 1
and the tile program's layer l sits at k = l + 1.

    ports        each participant enters through the bottom of its tile and leaves
                 through the top, a K pipe. The tile model calls a qubit's top and
                 bottom sides (the rows, J) its Z side: the side a control merges
                 through. lassynth names the faces the other way round, so the same
                 qubit is z_basis_direction "I" there; `check` is what settles it.
                 Each magic state consumed is a port too, the open cube its magic
                 tile on the layer of the merge, the pipe towards the route that
                 reaches it.
    stabilizers  the window's pairs in execution order, on a Choi state: a CX is a
                 CX, a T merge a Z spider on its qubit with one leg to the magic port
                 (the phase lives in the state, outside the Clifford skeleton).
    forbidden    layer 0 outside the input ports, the column of every bystander
                 qubit and idle magic tile, and a consumed magic tile away from its
                 port: what the window may not touch.
    geometry     the tile program's own pipes as given values. With them lassynth
                 only has to colour the pipes and place the correlation surfaces, so
                 a satisfiable answer says the spec and the tile model agree.

Port order, which is the column order of the stabilizers: the participants'
inputs by qubit, their outputs by qubit, the magic ports in execution order.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from ..circuit import Problem
from ..gltf import _route_path, _z_party, ancilla
from ..mapping import Coord
from ..program import MAGIC, Program
from .window import LAYERS_PER_STEP, Component, Window

QUBIT_Z = "I"
MAGIC_Z = "plane"


def layers(window: Window) -> int:
    """Layers the window spans in the tile program."""
    return LAYERS_PER_STEP * window.steps


def port_names(window: Window) -> List[str]:
    qs = window.participants
    return ([f"in q{q}" for q in qs] + [f"out q{q}" for q in qs]
            + [f"magic p{c.pair}" for c in window.components if c.magic is not None])


def stabilizers(window: Window, problem: Problem) -> List[str]:
    """Unsigned stabilizer generators over the ports, in canonical form."""
    import stim
    qs = window.participants
    n = len(qs)
    wire = {q: n + i for i, q in enumerate(qs)}
    circuit = stim.Circuit()
    for i in range(n):
        circuit.append("H", [i])
        circuit.append("CX", [i, n + i])
    m = 0
    for c in window.components:
        pair = problem.pairs[c.pair]
        if pair.is_magic:
            circuit.append("CX", [wire[pair.z], 2 * n + m])
            m += 1
        else:
            circuit.append("CX", [wire[pair.z], wire[pair.x]])
    sim = stim.TableauSimulator()
    sim.set_num_qubits(2 * n + m)
    sim.do(circuit)
    return [str(s)[1:].replace("_", "I") for s in sim.canonical_stabilizers()]


def _cube(window: Window, tile: Coord, k: int) -> Tuple[int, int, int]:
    return (tile[1] - window.box.c0, tile[0] - window.box.r0, k)


def _seats(window: Window, program: Program) -> Dict[int, Coord]:
    return program.steps[window.start].qubit_tiles()


def _magic_ports(window: Window, program: Program, depth: int) -> List[Tuple[Component, int, Coord]]:
    """(component, layer k, neighbour tile) per magic state consumed: the lower layer of
    its step, lowered to fit `depth` and to keep two uses of one tile on separate layers."""
    taken = set()
    out = []
    for c in reversed([c for c in window.components if c.magic is not None]):
        k = min(1 + LAYERS_PER_STEP * (c.step - window.start), depth)
        while (c.magic, k) in taken:
            k -= 1
        if k < 1:
            raise ValueError(f"depth {depth} cannot hold the magic states consumed at {c.magic}")
        taken.add((c.magic, k))
        (neighbour,) = [n for n in program.steps[c.step].neighbours()[c.magic] if n in c.tiles]
        out.append((c, k, neighbour))
    return list(reversed(out))


def _direction(src: Coord, dst: Coord) -> str:
    dr, dc = dst[0] - src[0], dst[1] - src[1]
    return {(0, 1): "+I", (0, -1): "-I", (1, 0): "+J", (-1, 0): "-J"}[(dr, dc)]


def spec(window: Window, program: Program, problem: Problem, depth: Optional[int] = None,
         qubit_z: str = QUBIT_Z, magic_z: str = MAGIC_Z) -> dict:
    """The lassynth specification of `window` at `depth` layers (its own by default).

    :param qubit_z: "I" | "J", lassynth's z_basis_direction of the qubit ports.
    :param magic_z: "K" | "plane", the axis a magic port's Z boundary faces: time, or the
        in-plane axis across its pipe.
    """
    depth = layers(window) if depth is None else depth
    if depth < 1:
        raise ValueError("a window needs at least one layer")
    seats = _seats(window, program)
    ports = []
    for q in window.participants:
        i, j, _ = _cube(window, seats[q], 0)
        ports.append({"location": [i, j, 0], "direction": "+K", "z_basis_direction": qubit_z, "function": f"in q{q}"})
    for q in window.participants:
        i, j, _ = _cube(window, seats[q], 0)
        ports.append({"location": [i, j, depth + 1], "direction": "-K", "z_basis_direction": qubit_z, "function": f"out q{q}"})
    open_cubes = {tuple(p["location"]) for p in ports[:len(window.participants)]}
    for c, k, neighbour in _magic_ports(window, program, depth):
        direction = _direction(c.magic, neighbour)
        across = "J" if direction[1] == "I" else "I"
        ports.append({"location": list(_cube(window, c.magic, k)), "direction": direction,
                      "z_basis_direction": "K" if magic_z == "K" else across, "function": f"magic p{c.pair}"})
        open_cubes.add(_cube(window, c.magic, k))

    forbidden = set()
    for tile in window.box.tiles():
        forbidden.add(_cube(window, tile, 0))
    reserved = list(window.bystanders.values()) + list(window.idle_magic) + [c.magic for c in window.components if c.magic]
    for tile in reserved:
        for k in range(depth + 1):
            forbidden.add(_cube(window, tile, k))
    forbidden -= open_cubes
    return {"max_i": window.box.width, "max_j": window.box.height, "max_k": depth + 1, "ports": ports,
            "stabilizers": stabilizers(window, problem),
            "optional": {"forbidden_cubes": [list(c) for c in sorted(forbidden)]}}


def pipes(window: Window, program: Program) -> Dict[str, set]:
    """The tile program's pipes inside the window, as {"I" | "J" | "K": {(i, j, k)}}: the
    pipe from cube (i, j, k) to its successor along that axis. Ports included."""
    out = {"I": set(), "J": set(), "K": set()}

    def add(a, b):
        lo, hi = min(a, b), max(a, b)
        axis = "IJK"[next(x for x in range(3) if lo[x] != hi[x])]
        out[axis].add(lo)

    depth = layers(window)
    seats = _seats(window, program)
    for q in window.participants:
        for k in range(depth + 1):
            add(_cube(window, seats[q], k), _cube(window, seats[q], k + 1))
    for c in window.components:
        step = program.steps[c.step]
        lower = 1 + LAYERS_PER_STEP * (c.step - window.start)
        upper = lower + 1
        path = _route_path(step, c.tiles, _z_party(step, c.tiles))
        if step.vertices[path[-1]].kind == MAGIC:
            for a, b in zip(path, path[1:]):
                add(_cube(window, a, lower), _cube(window, b, lower))
            continue
        turn = ancilla(path)
        for a, b in zip(path[:turn], path[1:turn + 1]):
            add(_cube(window, a, lower), _cube(window, b, lower))
        add(_cube(window, path[turn], lower), _cube(window, path[turn], upper))
        for a, b in zip(path[turn:], path[turn + 1:]):
            add(_cube(window, a, upper), _cube(window, b, upper))
    return out


def geometry(window: Window, program: Program) -> List[dict]:
    """Given values fixing every pipe of the volume to the tile program's and ruling
    out Y cubes. Only meaningful with the window's own depth."""
    mine = pipes(window, program)
    ni, nj, nk = window.box.width, window.box.height, layers(window) + 1
    vals = []
    for i in range(ni):
        for j in range(nj):
            for k in range(nk):
                for axis in "IJK":
                    vals.append({"array": f"Exist{axis}", "indices": [i, j, k], "value": int((i, j, k) in mine[axis])})
                vals.append({"array": "NodeY", "indices": [i, j, k], "value": 0})
    return vals
