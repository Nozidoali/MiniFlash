"""Hierarchical Clifford+T benchmarks from Qualtran bloqs.

    python3 scripts/qualtran_bloqs.py --list
    python3 scripts/qualtran_bloqs.py add-8 gf2mul-4 -o benchmarks/qualtran

A benchmark is `family-n`: a bloq family and its size. Each one is written as
`<name>.json`, the hierarchy (see miniflash/hierarchy.py), and `<name>.qasm`, the
same circuit flat, for compilers that take no hierarchy.

How a bloq becomes a definition. `decompose_bloq` gives its sub-bloqs and the
wires between them; every sub-bloq becomes a call of its own definition, built
once and shared by all its callers. Bookkeeping (split, join, partition, cast,
allocate, free) only renames wires and leaves no trace. A bloq Qualtran calls
atomic is turned into gates through its cirq operation: And is 4 T, Toffoli 7 T,
and And's uncomputation is a measurement with a conditional CZ. Two atomic bloqs
have no cirq operation and get one here: TwoBitCSwap is cirq's CSWAP, 7 T, and
SynthesizeLRCircuit, which Qualtran only counts, is the CNOTs of a Gauss-Jordan
elimination. A definition of one item is spliced into its callers, so CNOT is
the gate cx and not a call.

Qubits. A definition numbers its own qubits from 0, its inputs first. A qubit
Qualtran allocates inside takes the lowest free number, so freed workspace is
reused as early as it can be: the circuit is as narrow as this order allows, and
two sub-bloqs that could run side by side share workspace and wait for each other.

Needs qualtran and cirq. The system python3 has them; the project venv is Python
3.9 and does not.
"""
import argparse
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cirq
import numpy as np
from qualtran import Bloq, DecomposeNotImplementedError, DecomposeTypeError, LeftDangle, Register, Soquet
from qualtran.bloqs.basic_gates import TwoBitCSwap
from qualtran.bloqs.basic_gates.x_basis import _XVector
from qualtran.bloqs.basic_gates.z_basis import _ZVector
from qualtran.bloqs.bookkeeping._bookkeeping_bloq import _BookkeepingBloq
from qualtran.bloqs.gf_arithmetic.gf2_multiplication import SynthesizeLRCircuit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from miniflash.hierarchy import GATES, Def, Hierarchy, Item  # noqa: E402

Quregs = Dict[str, np.ndarray]   # register name -> local qubits, shape reg.shape + (reg.bitsize,)
QUARTER = {0: (), 1: ("t",), 2: ("s",), 3: ("s", "t"), 4: ("s", "s"), 5: ("s", "s", "t"), 6: ("sdg",), 7: ("tdg",)}


class Pool(cirq.QubitManager):
    """Local qubits of one definition: the lowest free index first."""

    def __init__(self):
        self.size = 0
        self.free = set()

    def qalloc(self, n, dim=2):
        out = []
        for _ in range(n):
            i = min(self.free) if self.free else self.size
            self.free.discard(i)
            self.size = max(self.size, i + 1)
            out.append(cirq.LineQubit(i))
        return out

    def qborrow(self, n, dim=2):
        return self.qalloc(n, dim)

    def qfree(self, qubits):
        self.free.update(q.x for q in qubits)


def translate(op: cirq.Operation) -> Optional[List[Tuple[str, tuple]]]:
    """A cirq operation as gates of the benchmark set, None when it is not one of them.
    Global phases are dropped."""
    g, qs = op.gate, op.qubits
    if isinstance(g, (cirq.IdentityGate, cirq.GlobalPhaseGate)):
        return []
    if isinstance(g, cirq.MeasurementGate) and len(qs) == 1:
        return [("measure", qs)]
    if isinstance(g, cirq.ResetChannel):
        return [("reset", qs)]
    if isinstance(g, cirq.HPowGate) and g.exponent == 1:
        return [("h", qs)]
    if isinstance(g, cirq.CXPowGate) and g.exponent == 1:
        return [("cx", qs)]
    if isinstance(g, cirq.CZPowGate) and g.exponent == 1:
        return [("h", qs[1:]), ("cx", qs), ("h", qs[1:])]
    if isinstance(g, (cirq.ZPowGate, cirq.XPowGate, cirq.YPowGate)) and not cirq.is_parameterized(g):
        k = float(g.exponent) * 4
        if abs(k - round(k)) > 1e-9:
            return None
        z = [(name, qs) for name in QUARTER[round(k) % 8]]
        if isinstance(g, cirq.ZPowGate):
            return z
        x = [("x", qs)] if round(k) % 8 == 4 else [("h", qs)] + z + [("h", qs)]
        return x if isinstance(g, cirq.XPowGate) else [("sdg", qs)] + x + [("s", qs)]
    return None


def _bare(op: cirq.Operation) -> cirq.Operation:
    return op.without_classical_controls() if isinstance(op, cirq.ClassicallyControlledOperation) else op


def gates_of(op: cirq.Operation, pool: Pool) -> List[Item]:
    """`op` decomposed into the benchmark's gates, on local qubit numbers. A classically
    controlled gate is conditioned on the qubit its measurement key was read from."""
    out: List[Item] = []
    measured: Dict[str, int] = {}
    for o in cirq.decompose(op, keep=lambda o: translate(_bare(o)) is not None, context=cirq.DecompositionContext(pool)):
        cond = None
        if isinstance(o, cirq.ClassicallyControlledOperation):
            (control,) = o.classical_controls
            (key,) = control.keys
            cond = measured[key.name]
        for name, qs in translate(_bare(o)):
            if name == "measure":
                measured[o.gate.key] = qs[0].x
            out.append((name, tuple(q.x for q in qs), cond))
    return out


def _effect(bloq: Bloq) -> Optional[Tuple[str, ...]]:
    """The gates returning the qubit an effect bloq discards to |0>, None for any other bloq."""
    if isinstance(bloq, _ZVector) and not bloq.state:
        return ("x",) if bloq.bit else ()
    if isinstance(bloq, _XVector) and not bloq.state:
        return ("h", "x") if bloq.bit else ("h",)
    return None


def linear(matrix) -> List[Tuple[int, int]]:
    """CNOTs (control, target), in order, taking q to matrix @ q over GF(2): the row
    operations of a Gauss-Jordan elimination, undone last to first."""
    m = np.array(matrix, dtype=int) % 2
    ops = []
    for c in range(len(m)):
        if not m[c, c]:
            pivot = next(r for r in range(c + 1, len(m)) if m[r, c])
            m[c] ^= m[pivot]
            ops.append((pivot, c))
        for r in range(len(m)):
            if r != c and m[r, c]:
                m[r] ^= m[c]
                ops.append((c, r))
    return ops[::-1]


def _labels(reg: Register) -> List[str]:
    return ["_".join([reg.name, *map(str, idx), *([str(b)] if reg.bitsize > 1 else [])])
            for idx in reg.all_idxs() for b in range(reg.bitsize)]


def _gather(reg: Register, soq, wires: Dict[Soquet, np.ndarray]) -> np.ndarray:
    out = np.empty(reg.shape + (reg.bitsize,), dtype=object)
    for idx in reg.all_idxs():
        out[idx] = wires[np.asarray(soq)[idx]]
    return out


def _qubits(quregs: Quregs) -> set:
    return {q for qureg in quregs.values() for q in np.asarray(qureg).flatten()}


class Builder:
    """Definitions of bloqs, each built once."""

    def __init__(self):
        self.defs: Dict[Bloq, Def] = {}
        self.names = set(GATES)

    def name(self, bloq: Bloq) -> str:
        base = re.sub(r"[^0-9a-z]+", "_", str(bloq).replace("†", " dag").lower()).strip("_") or "bloq"
        if not base[0].isalpha():
            base = "b_" + base
        name, k = base, 1
        while name in self.names:
            k += 1
            name = f"{base}_{k}"
        self.names.add(name)
        return name

    def define(self, bloq: Bloq) -> Def:
        if bloq in self.defs:
            return self.defs[bloq]
        pool = Pool()
        quregs = {reg.name: np.array(pool.qalloc(reg.total_bits()), dtype=object).reshape(reg.shape + (reg.bitsize,))
                  for reg in bloq.signature.lefts()}
        ins = tuple(range(pool.size))
        body: List[Item] = []
        try:
            cbloq = bloq.decompose_bloq()
        except (DecomposeTypeError, DecomposeNotImplementedError):
            outs = self.apply(bloq, quregs, pool, body)
        else:
            outs = self.composite(cbloq, quregs, pool, body)
        pool.qfree(_qubits(quregs) - _qubits(outs))
        out_ids = tuple(q.x for reg in bloq.signature.rights() for q in np.asarray(outs[reg.name]).flatten())
        dirty = set(range(pool.size)) - pool.free - set(out_ids)
        if dirty:
            raise ValueError(f"{bloq}: local qubits {sorted(dirty)} are neither freed nor returned")
        labels: List[Optional[str]] = [None] * pool.size
        for regs, ids in ((bloq.signature.lefts(), ins), (bloq.signature.rights(), out_ids)):
            for label, i in zip([label for reg in regs for label in _labels(reg)], ids):
                if labels[i] is None:
                    while label in labels:
                        label += "_"
                    labels[i] = label
        for i in range(pool.size):
            if labels[i] is None:
                label = f"anc_{i}"
                while label in labels:
                    label += "_"
                labels[i] = label
        self.defs[bloq] = Def(self.name(bloq), " ".join(repr(bloq).split())[:200], tuple(labels), ins, out_ids, tuple(body))
        return self.defs[bloq]

    def apply(self, bloq: Bloq, quregs: Quregs, pool: Pool, body: List[Item]) -> Quregs:
        """Append the gates of an atomic or bookkeeping bloq to `body`; returns its output registers."""
        effect = _effect(bloq)
        if effect is not None:
            (q,) = _qubits(quregs)
            body += [(g, (q.x,), None) for g in effect]
            return {}
        if isinstance(bloq, SynthesizeLRCircuit):
            qs = quregs["q"].flatten()
            cnots = linear(bloq.matrix)
            body += [("cx", (qs[c].x, qs[t].x), None) for c, t in (cnots[::-1] if bloq.is_adjoint else cnots)]
            return quregs
        if isinstance(bloq, TwoBitCSwap):
            op, outs = cirq.CSWAP(*(q for name in ("ctrl", "x", "y") for q in quregs[name].flatten())), quregs
        else:
            op, outs = bloq.as_cirq_op(pool, **quregs)
        if op is not None:
            try:
                body += gates_of(op, pool)
            except ValueError as error:
                raise ValueError(f"{bloq!r} is not Clifford+T: {str(error)[:200]}") from None
        return outs

    def call(self, bloq: Bloq, quregs: Quregs, pool: Pool, body: List[Item]) -> Quregs:
        """Append a call of `bloq`'s definition on `quregs`, lending free qubits for the rest
        of its local qubits; a definition of one item is spliced in instead."""
        d = self.define(bloq)
        args = [q for reg in bloq.signature.lefts() for q in quregs[reg.name].flatten()]
        args += pool.qalloc(len(d.qubits) - len(args))
        ids = tuple(q.x for q in args)
        if len(d.body) <= 1:
            body += [(g, tuple(ids[i] for i in qs), None if cond is None else ids[cond]) for g, qs, cond in d.body]
        else:
            body.append((d.name, ids, None))
        outs, k = {}, 0
        for reg in bloq.signature.rights():
            n = reg.total_bits()
            outs[reg.name] = np.array([args[i] for i in d.outs[k:k + n]], dtype=object).reshape(reg.shape + (reg.bitsize,))
            k += n
        pool.qfree(set(args) - _qubits(outs))
        return outs

    def composite(self, cbloq, quregs: Quregs, pool: Pool, body: List[Item]) -> Quregs:
        """Walk a decomposition in topological order, wires carrying local qubits."""
        wires = {Soquet(LeftDangle, reg, idx): quregs[reg.name][idx]
                 for reg in cbloq.signature.lefts() for idx in reg.all_idxs()}
        for binst, in_soqs, out_soqs in cbloq.iter_bloqsoqs():
            bloq = binst.bloq
            given = {reg.name: _gather(reg, in_soqs[reg.name], wires) for reg in bloq.signature.lefts()}
            if isinstance(bloq, _BookkeepingBloq):
                outs = self.apply(bloq, given, pool, body)
                pool.qfree(_qubits(given) - _qubits(outs))
            else:
                outs = self.call(bloq, given, pool, body)
            for reg, soq in zip(bloq.signature.rights(), out_soqs):
                for idx in reg.all_idxs():
                    wires[np.asarray(soq)[idx]] = np.asarray(outs[reg.name])[idx]
        final = cbloq.final_soqs()
        return {reg.name: _gather(reg, final[reg.name], wires) for reg in cbloq.signature.rights()}


def hierarchy(bloq: Bloq, source: str = "") -> Hierarchy:
    """The hierarchy of `bloq`: its definition and every definition it reaches."""
    builder = Builder()
    root = builder.define(bloq)
    by_name = {d.name: d for d in builder.defs.values()}
    reached, stack = {root.name}, [root.name]
    while stack:
        for g, _, _ in by_name[stack.pop()].body:
            if g in by_name and g not in reached:
                reached.add(g)
                stack.append(g)
    return Hierarchy([d for d in builder.defs.values() if d.name in reached], root.name, source)


def _prime(n: int) -> int:
    """The largest prime below 2**n."""
    p = 2 ** n - 1
    while any(p % d == 0 for d in range(2, int(p ** 0.5) + 1)):
        p -= 1
    return p


def _add(n):
    from qualtran import QUInt
    from qualtran.bloqs.arithmetic import Add
    return Add(QUInt(n))


def _modadd(n):
    from qualtran.bloqs.mod_arithmetic import ModAdd
    return ModAdd(n, _prime(n))


def _modmul(n):
    from qualtran import QUInt
    from qualtran.bloqs.mod_arithmetic import CModMulK
    return CModMulK(QUInt(n), k=3, mod=_prime(n))


def _modexp(n):
    from qualtran.bloqs.cryptography.rsa import ModExp
    return ModExp(base=3, mod=_prime(n), exp_bitsize=n, x_bitsize=n)


def _gf2mul(n):
    from qualtran.bloqs.gf_arithmetic import GF2Multiplication
    return GF2Multiplication(n)


def _gf2inv(n):
    from qualtran.bloqs.gf_arithmetic import GF2Inverse
    return GF2Inverse(n)


def _multiand(n):
    from qualtran.bloqs.mcmt import MultiAnd
    return MultiAnd((1,) * n)


def _lte(n):
    from qualtran.bloqs.arithmetic import LessThanEqual
    return LessThanEqual(n, n)


def _qrom(n):
    from qualtran.bloqs.data_loading.qrom import QROM
    data = np.random.default_rng(0).integers(0, 2 ** n, size=2 ** n)
    return QROM.build_from_data(data)


def _hubbard(n):
    from qualtran.bloqs.chemistry.hubbard_model.qubitization import SelectHubbard
    return SelectHubbard(n, n)


FAMILIES: Dict[str, Tuple[Callable[[int], Bloq], str]] = {
    "add": (_add, "n-bit adder, a + b into b (Gidney)"),
    "modadd": (_modadd, "n-bit modular adder, modulus the largest prime below 2^n"),
    "modmul": (_modmul, "controlled n-bit modular multiplication by 3, same modulus"),
    "modexp": (_modexp, "n-bit modular exponentiation 3^e, n-bit exponent, same modulus: n controlled multiplications"),
    "gf2mul": (_gf2mul, "GF(2^n) multiplication, out of place"),
    "gf2inv": (_gf2inv, "GF(2^n) inversion by Itoh-Tsujii: squarings and multiplications"),
    "multiand": (_multiand, "AND of n controls with its ladder of ancillas"),
    "lte": (_lte, "n-bit comparison a <= b into one qubit"),
    "qrom": (_qrom, "table lookup: 2^n entries of n bits, seeded random data"),
    "hubbard": (_hubbard, "SELECT of the n x n Hubbard model (qubitization)"),
}


def build(name: str) -> Hierarchy:
    """The benchmark `family-n`."""
    family, _, size = name.rpartition("-")
    if family not in FAMILIES or not size.isdigit():
        raise KeyError(f"unknown benchmark {name!r}; families: {', '.join(FAMILIES)}")
    bloq = FAMILIES[family][0](int(size))
    from qualtran._version import __version__
    return hierarchy(bloq, f"{name}: {' '.join(repr(bloq).split())[:120]}, qualtran {__version__}")


def describe(h: Hierarchy) -> str:
    """One line per definition: how often it appears, its qubits, and its T and CX gates."""
    seen = h.instances()
    lines = [f"{'definition':<24}{'calls':>7}{'in':>5}{'out':>5}{'work':>6}{'t':>8}{'cx':>8}  calls"]
    for d in reversed(list(h.defs.values())):
        c = h.counts(d.name)
        callees = ", ".join(f"{k} x{v}" for k, v in h.calls(d.name).items())
        lines.append(f"{d.name:<24}{seen.get(d.name, 0):>7}{len(d.ins):>5}{len(d.outs):>5}{len(d.workspace):>6}"
                     f"{c.get('t', 0) + c.get('tdg', 0):>8}{c.get('cx', 0):>8}  {callees}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="benchmarks, each family-n")
    ap.add_argument("--list", action="store_true", help="list the families")
    ap.add_argument("-o", "--out", default="benchmarks/qualtran", help="directory for the .json and .qasm files")
    a = ap.parse_args()
    if a.list or not a.names:
        for family, (_, text) in FAMILIES.items():
            print(f"{family:<10}{text}")
        return
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in a.names:
        h = build(name)
        h.save(out / f"{name}.json")
        (out / f"{name}.qasm").write_text(h.to_qasm())
        print(f"{name}: {h.n_qubits} qubits, {len(h.problem.pairs)} pairs, depth {h.problem.depth}, "
              f"{len(h.defs)} definitions, {sum(h.instances().values())} calls")
        print(describe(h))


if __name__ == "__main__":
    main()
