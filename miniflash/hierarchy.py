"""Hierarchy: a Clifford+T circuit together with the call tree it was built from.

A hierarchical benchmark is a set of definitions, one per subroutine (a Qualtran
bloq, see scripts/qualtran_bloqs.py). A definition names its local qubits and
lists its body in program order: gates on local qubits and calls of other
definitions. The local qubits alive at entry are its inputs and those alive at
exit its outputs; the rest is workspace, which the body takes clean and returns
clean, so a caller may lend any free qubit for it.

Flattening the root gives

    problem   the pair DAG the compiler takes, pairs in program order
    tree      one Node per call: its definition, its qubits, and the pairs it spans,
              a contiguous range because a call is flattened in place

so every cut through the tree is a partition of the problem. `calls` is the call
graph, how often a definition calls another, and `Def.preds` the dependencies
between the items of one body.

Gates are h, x, s, sdg, t, tdg and cx, plus measure and reset where a subroutine
uncomputes by measurement. A gate may carry a condition: the qubit whose last
measurement decides whether it runs. The problem and the flat QASM keep a
conditional gate as if it always ran, the worst case a static schedule has to
provide for, and leave measure and reset out.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .circuit import MAGIC, Pair, Problem

GATES = ("h", "x", "s", "sdg", "t", "tdg", "cx", "measure", "reset")
Item = Tuple[str, Tuple[int, ...], Optional[int]]   # (gate or definition, qubits, condition)


@dataclass(frozen=True)
class Def:
    """One subroutine. `ins` and `outs` index `qubits`; `bloq` describes where it came from."""
    name: str
    bloq: str
    qubits: Tuple[str, ...]
    ins: Tuple[int, ...]
    outs: Tuple[int, ...]
    body: Tuple[Item, ...]

    @property
    def workspace(self) -> Tuple[int, ...]:
        """Local qubits that are neither input nor output."""
        pins = set(self.ins) | set(self.outs)
        return tuple(i for i in range(len(self.qubits)) if i not in pins)

    def preds(self) -> Tuple[Tuple[int, ...], ...]:
        """Program-order dependencies between the items of the body: each waits for the
        last earlier item on each of its qubits, and for the measurement it is conditioned on."""
        last: Dict[int, int] = {}
        out = []
        for i, (_, qs, cond) in enumerate(self.body):
            used = qs if cond is None else qs + (cond,)
            out.append(tuple(sorted({last[q] for q in used if q in last})))
            for q in qs:
                last[q] = i
        return tuple(out)

    def to_dict(self) -> dict:
        return {"name": self.name, "bloq": self.bloq, "qubits": list(self.qubits), "in": list(self.ins), "out": list(self.outs),
                "body": [[g, list(qs)] if cond is None else [g, list(qs), cond] for g, qs, cond in self.body]}

    @classmethod
    def from_dict(cls, d: dict) -> "Def":
        body = tuple((item[0], tuple(item[1]), item[2] if len(item) > 2 else None) for item in d["body"])
        return cls(d["name"], d["bloq"], tuple(d["qubits"]), tuple(d["in"]), tuple(d["out"]), body)


@dataclass
class Node:
    """One call in the flat circuit: `qubits` in the order of the definition's, and the
    pairs start..stop-1 of the problem, its children's included."""
    name: str
    qubits: Tuple[int, ...]
    start: int
    stop: int = 0
    children: List["Node"] = field(default_factory=list)

    @property
    def pairs(self) -> range:
        return range(self.start, self.stop)

    def walk(self) -> Iterator["Node"]:
        """This node and everything below it, parents first."""
        yield self
        for child in self.children:
            yield from child.walk()


class Hierarchy:
    def __init__(self, defs: Iterable[Def], root: str, source: str = ""):
        """:param defs: the definitions, in any order.
        :param root: name of the definition the circuit is; its local qubits are the circuit's.
        :param source: free text on where the benchmark came from."""
        self.defs: Dict[str, Def] = {d.name: d for d in defs}
        self.root, self.source = root, source
        for d in self.defs.values():
            if d.name in GATES:
                raise ValueError(f"definition {d.name!r} shadows a gate")
            for g, qs, cond in d.body:
                if g not in self.defs and g not in GATES:
                    raise ValueError(f"{d.name}: unknown gate or definition {g!r}")
                if g in self.defs and len(qs) != len(self.defs[g].qubits):
                    raise ValueError(f"{d.name}: {g} takes {len(self.defs[g].qubits)} qubits, not {len(qs)}")
                if len(set(qs)) != len(qs) or not all(0 <= q < len(d.qubits) for q in qs):
                    raise ValueError(f"{d.name}: bad qubits {qs} for {g}")
        self.gates: List[Item] = []
        pairs: List[Pair] = []
        dropped: Counter = Counter()
        self.tree = self._flatten(root, tuple(range(len(self.defs[root].qubits))), pairs, dropped)
        self.problem = Problem.from_pairs(len(self.defs[root].qubits), pairs, dict(dropped))

    def _flatten(self, name: str, args: Tuple[int, ...], pairs: List[Pair], dropped: Counter) -> Node:
        node = Node(name, args, len(pairs))
        for g, ids, cond in self.defs[name].body:
            qs = tuple(args[i] for i in ids)
            if g in self.defs:
                node.children.append(self._flatten(g, qs, pairs, dropped))
                continue
            self.gates.append((g, qs, None if cond is None else args[cond]))
            if g == "cx":
                pairs.append(Pair(qs[0], qs[1], "cx"))
            elif g in ("t", "tdg"):
                pairs.append(Pair(qs[0], MAGIC, g))
            else:
                dropped[g] += 1
        node.stop = len(pairs)
        return node

    @property
    def n_qubits(self) -> int:
        return self.problem.n_qubits

    def calls(self, name: str) -> Dict[str, int]:
        """The call graph at `name`: {definition it calls: how often}."""
        return dict(Counter(g for g, _, _ in self.defs[name].body if g in self.defs))

    def counts(self, name: str) -> Dict[str, int]:
        """Gates of one call of `name`, everything it calls included."""
        memo: Dict[str, Counter] = {}

        def of(n: str) -> Counter:
            if n not in memo:
                total: Counter = Counter()
                for g, _, _ in self.defs[n].body:
                    total.update(of(g) if g in self.defs else {g: 1})
                memo[n] = total
            return memo[n]
        return dict(of(name))

    def instances(self) -> Dict[str, int]:
        """How many times each definition appears in the flat circuit."""
        return dict(Counter(node.name for node in self.tree.walk()))

    def to_qasm(self) -> str:
        """The flat circuit as OpenQASM 2: conditional gates unconditional, no measure or reset."""
        lines = ["OPENQASM 2.0;", 'include "qelib1.inc";']
        if self.source:
            lines.append(f"// {self.source}")
        lines.append(f"qreg q[{self.n_qubits}];")
        for g, qs, _ in self.gates:
            if g not in ("measure", "reset"):
                lines.append(f"{g} {','.join(f'q[{q}]' for q in qs)};")
        return "\n".join(lines) + "\n"

    def save(self, path) -> None:
        body = {"source": self.source, "root": self.root, "defs": [d.to_dict() for d in self.defs.values()]}
        Path(path).write_text(json.dumps(body, separators=(",", ":")) + "\n")

    @classmethod
    def load(cls, path) -> "Hierarchy":
        body = json.loads(Path(path).read_text())
        return cls([Def.from_dict(d) for d in body["defs"]], body["root"], body.get("source", ""))
