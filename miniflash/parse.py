"""Circuit ingestion.

Reads OpenQASM 2.0 (or a qiskit ``QuantumCircuit``) and normalizes it to
the accepted Clifford+T gate set: ``h``, ``cx``, ``x``, ``s``, ``sdg``,
``t``, ``tdg``, ``measure`` and ``barrier`` pass through unchanged, while
``z``, ``y``, ``cz``, ``swap``, ``id``, ``rz`` at quarter turns and
``ccx`` (standard 7-T decomposition) are expanded in place. A gate
outside this set raises ``ValueError`` — nothing is silently dropped.

Example::

    import miniflash as flash

    circuit = flash.parse("benchmarks/algorithms/ghz8.qasm")
"""
import math

from qiskit import qasm2
from qiskit.circuit import QuantumCircuit

ACCEPTED_GATES = ("h", "cx", "x", "s", "sdg", "t", "tdg", "measure", "barrier")
CLIFFORD_GATES = ("h", "cx", "x", "s", "sdg")


_EXPANSIONS = {
    "id": (),
    "z": (("h", (0,)), ("x", (0,)), ("h", (0,))),
    "y": (("h", (0,)), ("x", (0,)), ("h", (0,)), ("x", (0,))),
    "cz": (("h", (1,)), ("cx", (0, 1)), ("h", (1,))),
    "swap": (("cx", (0, 1)), ("cx", (1, 0)), ("cx", (0, 1))),
    "ccx": (("h", (2,)), ("cx", (1, 2)), ("tdg", (2,)), ("cx", (0, 2)), ("t", (2,)), ("cx", (1, 2)), ("tdg", (2,)), ("cx", (0, 2)), ("t", (1,)), ("t", (2,)), ("h", (2,)), ("cx", (0, 1)), ("t", (0,)), ("tdg", (1,)), ("cx", (0, 1))),
}

_RZ_QUARTER_TURNS = {
    0: (),
    1: (("s", (0,)),),
    2: (("h", (0,)), ("x", (0,)), ("h", (0,))),
    3: (("sdg", (0,)),),
}


def _rz_replacement(operation):
    angle = float(operation.params[0])
    steps = round(angle / (math.pi / 2))
    if abs(angle - steps * math.pi / 2) > 1e-9:
        return None
    return _RZ_QUARTER_TURNS[steps % 4]


def _expand_gates(circuit: QuantumCircuit) -> QuantumCircuit:
    if all(instruction.operation.name not in _EXPANSIONS and instruction.operation.name != "rz" for instruction in circuit.data):
        return circuit
    expanded = QuantumCircuit(*circuit.qregs, *circuit.cregs)
    for instruction in circuit.data:
        if instruction.operation.name == "rz":
            replacement = _rz_replacement(instruction.operation)
        else:
            replacement = _EXPANSIONS.get(instruction.operation.name)
        if replacement is None:
            expanded.append(instruction.operation, instruction.qubits, instruction.clbits)
        else:
            for name, positions in replacement:
                getattr(expanded, name)(*(instruction.qubits[position] for position in positions))
    return expanded


def parse(path) -> QuantumCircuit:
    """Load and normalize a circuit to the accepted gate set (``h``/``cx``/``x``/``s``/``sdg``/``t``/``tdg``/``measure``/``barrier``).

    :param path: str | Path to a .qasm file.
    :returns: QuantumCircuit with ``z``/``y``/``cz``/``swap``/``id``/``rz(k*pi/2)`` expanded.
    :raises ValueError: on unsupported gates.
    """
    circuit = _expand_gates(qasm2.load(str(path)))
    for instruction in circuit.data:
        name = instruction.operation.name
        if name not in ACCEPTED_GATES:
            raise ValueError(f"unsupported construct '{name}' (accepted: h/cx/x/s/sdg/t/tdg/measure/barrier; z/y/cz/swap/id auto-expand)")
    return circuit
