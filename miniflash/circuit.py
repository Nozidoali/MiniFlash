"""Input side: a Clifford+T circuit becomes a Problem, a DAG of ordered pairs.

A pair is (z, x): the party attached through its Z boundary (top/bottom side) and
the party attached through its X boundary (left/right side). A CX is (control,
target). A T gate is (qubit, MAGIC): the magic state plays the X party. Cliffords
are free in the execution model, so the reader drops them but counts what it dropped.

Dependencies are program order per qubit. MAGIC never induces a dependency: every
magic state is fresh. `preds[i]` lists the pairs that must run before pair i.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

MAGIC = -1
CLIFFORD = {"h", "x", "y", "z", "s", "sdg", "sx", "sxdg", "id"}


@dataclass(frozen=True)
class Pair:
    z: int
    x: int
    tag: str  # "cx" | "t" | "tdg"

    @property
    def is_magic(self) -> bool:
        return self.x == MAGIC

    @property
    def qubits(self) -> Tuple[int, ...]:
        return (self.z,) if self.is_magic else (self.z, self.x)

    def __str__(self) -> str:
        return f"{self.tag} q{self.z}" if self.is_magic else f"{self.tag} (q{self.z}, q{self.x})"


@dataclass(frozen=True)
class Problem:
    n_qubits: int
    pairs: Tuple[Pair, ...]
    preds: Tuple[Tuple[int, ...], ...]
    dropped: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if self.n_qubits <= 0:
            raise ValueError("a problem needs at least one qubit")
        if len(self.preds) != len(self.pairs):
            raise ValueError("one predecessor list per pair")
        for i, p in enumerate(self.pairs):
            for q in p.qubits:
                if not 0 <= q < self.n_qubits:
                    raise ValueError(f"pair {i} uses qubit {q} outside qreg q[{self.n_qubits}]")
            if not p.is_magic and p.z == p.x:
                raise ValueError(f"pair {i} joins q{p.z} with itself")
            if p.is_magic != (p.tag != "cx"):
                raise ValueError(f"pair {i}: tag {p.tag} does not match its parties")
            for j in self.preds[i]:
                if not 0 <= j < i:
                    raise ValueError(f"pair {i} depends on {j}, which is not earlier")

    @classmethod
    def from_pairs(cls, n_qubits: int, pairs: Sequence[Pair], dropped: Optional[Dict[str, int]] = None) -> "Problem":
        """Program-order dependencies: each pair waits for the last earlier pair on each of its qubits."""
        last: Dict[int, int] = {}
        preds = []
        for i, p in enumerate(pairs):
            preds.append(tuple(sorted({last[q] for q in p.qubits if q in last})))
            for q in p.qubits:
                last[q] = i
        return cls(n_qubits, tuple(pairs), tuple(preds), dict(dropped or {}))

    @classmethod
    def builder(cls, n_qubits: int) -> "Builder":
        """Incremental construction: Problem.builder(4).add_cx(0, 1).add_t(1).build()."""
        return Builder(n_qubits)

    @property
    def qubits(self) -> range:
        return range(self.n_qubits)

    def succs(self) -> Tuple[Tuple[int, ...], ...]:
        out: List[List[int]] = [[] for _ in self.pairs]
        for i, ps in enumerate(self.preds):
            for j in ps:
                out[j].append(i)
        return tuple(tuple(s) for s in out)

    def layers(self) -> List[List[int]]:
        """ASAP layers: pair i sits one layer below its deepest predecessor."""
        level = [0] * len(self.pairs)
        for i, ps in enumerate(self.preds):
            level[i] = max((level[j] + 1 for j in ps), default=0)
        out: List[List[int]] = [[] for _ in range(max(level, default=-1) + 1)]
        for i, l in enumerate(level):
            out[l].append(i)
        return out

    @property
    def depth(self) -> int:
        return len(self.layers())

    def interaction(self) -> Dict[Tuple[int, int], int]:
        """Interaction graph: {(a, b): number of CX pairs joining a and b}, a < b. T pairs
        touch a single qubit and add no edge."""
        w: Dict[Tuple[int, int], int] = {}
        for p in self.pairs:
            if not p.is_magic:
                e = (min(p.z, p.x), max(p.z, p.x))
                w[e] = w.get(e, 0) + 1
        return w

    def criticality(self) -> List[int]:
        """Length of the longest dependency chain starting at each pair, itself included."""
        crit = [1] * len(self.pairs)
        for i in range(len(self.pairs) - 1, -1, -1):
            for j in self.preds[i]:
                crit[j] = max(crit[j], crit[i] + 1)
        return crit


class Builder:
    """Collects pairs in program order; `build` derives the DAG."""

    def __init__(self, n_qubits: int):
        self.n_qubits = n_qubits
        self.pairs: List[Pair] = []

    def add_cx(self, control: int, target: int) -> "Builder":
        self.pairs.append(Pair(control, target, "cx"))
        return self

    def add_t(self, qubit: int) -> "Builder":
        self.pairs.append(Pair(qubit, MAGIC, "t"))
        return self

    def add_tdg(self, qubit: int) -> "Builder":
        self.pairs.append(Pair(qubit, MAGIC, "tdg"))
        return self

    def build(self) -> Problem:
        return Problem.from_pairs(self.n_qubits, self.pairs)


def from_circuit(circuit) -> Problem:
    """Pairs of a normalized Clifford+T ``QuantumCircuit`` (see :func:`miniflash.parse.parse`)."""
    pairs: List[Pair] = []
    dropped: Counter = Counter()
    for instruction in circuit.data:
        op = instruction.operation.name
        qs = [circuit.find_bit(q).index for q in instruction.qubits]
        if op == "cx":
            pairs.append(Pair(qs[0], qs[1], "cx"))
        elif op in ("t", "tdg"):
            pairs.append(Pair(qs[0], MAGIC, op))
        elif op in CLIFFORD or op in ("measure", "barrier"):
            dropped[op] += 1
        else:
            raise ValueError(f"unsupported gate {op}")
    return Problem.from_pairs(circuit.num_qubits, pairs, dict(dropped))


def read_qasm(path) -> Problem:
    """Parse an OpenQASM 2 file with :func:`miniflash.parse.parse` and extract its pairs."""
    from .parse import parse
    return from_circuit(parse(path))


def to_dot(problem: Problem, path=None) -> str:
    """Graphviz source for the pair DAG; ranks follow ASAP layers, edges carry the shared qubit."""
    lines = ["digraph problem {", "  rankdir=TB; node [shape=box, style=filled, fontname=Helvetica];"]
    for layer in problem.layers():
        names = " ".join(f"p{i}" for i in layer)
        lines.append(f"  {{ rank=same; {names}; }}")
    for i, p in enumerate(problem.pairs):
        fill = "#add2c2" if p.is_magic else "#c9bdd3"
        lines.append(f'  p{i} [label="{i}: {p}", fillcolor="{fill}"];')
    for i, ps in enumerate(problem.preds):
        for j in ps:
            shared = sorted(set(problem.pairs[i].qubits) & set(problem.pairs[j].qubits))
            label = ",".join(f"q{q}" for q in shared)
            lines.append(f'  p{j} -> p{i} [label="{label}"];')
    lines.append("}")
    src = "\n".join(lines) + "\n"
    if path is not None:
        Path(path).write_text(src)
    return src
