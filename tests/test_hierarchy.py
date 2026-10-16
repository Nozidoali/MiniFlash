import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from miniflash import MAGIC, Def, Hierarchy, Pair, make, read_qasm, verify

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = sorted((ROOT / "benchmarks" / "qualtran").glob("*.json"))

try:    # the generator needs qualtran and cirq, which the Python 3.9 venv cannot install
    sys.path.insert(0, str(ROOT / "scripts"))
    import qualtran_bloqs
except ImportError:
    qualtran_bloqs = None
needs_qualtran = pytest.mark.skipif(qualtran_bloqs is None, reason="needs qualtran and cirq: run under the system python3")


def small():
    """A root with gates of its own around two calls of `cell`, which borrows one qubit."""
    cell = Def("cell", "hand-made", ("a", "b", "w"), (0, 1), (0, 1),
               (("cx", (0, 2), None), ("cx", (2, 1), None), ("cx", (0, 2), None)))
    root = Def("root", "hand-made", ("x0", "x1", "x2", "anc"), (0, 1, 2), (0, 1, 2),
               (("h", (0,), None), ("cell", (0, 1, 3), None), ("t", (1,), None), ("cell", (1, 2, 3), None), ("cx", (2, 0), None)))
    return Hierarchy([cell, root], "root", "two cells")


def test_flattening_gives_the_problem_and_a_tree_of_pair_ranges():
    h = small()
    assert h.n_qubits == 4 and len(h.gates) == 9
    assert h.problem.pairs == (Pair(0, 3, "cx"), Pair(3, 1, "cx"), Pair(0, 3, "cx"), Pair(1, MAGIC, "t"),
                               Pair(1, 3, "cx"), Pair(3, 2, "cx"), Pair(1, 3, "cx"), Pair(2, 0, "cx"))
    assert h.problem.dropped == {"h": 1}
    assert (h.tree.name, h.tree.pairs) == ("root", range(0, 8))
    first, second = h.tree.children
    assert (first.name, first.qubits, first.pairs) == ("cell", (0, 1, 3), range(0, 3))
    assert (second.name, second.qubits, second.pairs) == ("cell", (1, 2, 3), range(4, 7))
    assert [n.name for n in h.tree.walk()] == ["root", "cell", "cell"]


def test_call_graph_counts_and_body_dependencies():
    h = small()
    assert h.calls("root") == {"cell": 2} and h.calls("cell") == {}
    assert h.counts("root") == {"h": 1, "cx": 7, "t": 1} and h.counts("cell") == {"cx": 3}
    assert h.instances() == {"root": 1, "cell": 2}
    assert h.defs["cell"].workspace == (2,) and h.defs["root"].workspace == (3,)
    # the second cell waits for the first through the borrowed qubit and for the T through x1
    assert h.defs["root"].preds() == ((), (0,), (1,), (1, 2), (1, 3))


def test_conditional_gates_stay_in_the_problem_and_measurements_leave_the_qasm():
    undo = Def("undo", "hand-made", ("a", "b", "t"), (0, 1, 2), (0, 1),
               (("h", (2,), None), ("measure", (2,), None), ("cx", (0, 1), 2), ("reset", (2,), None)))
    root = Def("root", "hand-made", ("p", "q", "r"), (0, 1, 2), (0, 1), (("undo", (1, 0, 2), None),))
    h = Hierarchy([undo, root], "root")
    assert h.gates == [("h", (2,), None), ("measure", (2,), None), ("cx", (1, 0), 2), ("reset", (2,), None)]
    assert h.problem.pairs == (Pair(1, 0, "cx"),)
    assert undo.preds() == ((), (0,), (1,), (1,))
    assert [line for line in h.to_qasm().splitlines() if not line.startswith(("OPENQASM", "include"))] == \
        ["qreg q[3];", "h q[2];", "cx q[1],q[0];"]


def test_save_load_and_flat_qasm_round_trip(tmp_path):
    h = small()
    h.save(tmp_path / "small.json")
    again = Hierarchy.load(tmp_path / "small.json")
    assert again.defs == h.defs and again.root == h.root and again.source == h.source
    assert again.problem == h.problem
    (tmp_path / "small.qasm").write_text(h.to_qasm())
    flat = read_qasm(tmp_path / "small.qasm")
    assert flat.n_qubits == 4 and Counter(flat.pairs) == Counter(h.problem.pairs)


def test_bad_definitions_are_rejected():
    cell = Def("cell", "", ("a", "b"), (0, 1), (0, 1), (("cx", (0, 1), None),))
    with pytest.raises(ValueError):
        Hierarchy([cell, Def("root", "", ("a", "b"), (0, 1), (0, 1), (("ccx", (0, 1), None),))], "root")
    with pytest.raises(ValueError):
        Hierarchy([cell, Def("root", "", ("a", "b", "c"), (0, 1, 2), (0, 1, 2), (("cell", (0, 1, 2), None),))], "root")
    with pytest.raises(ValueError):
        Hierarchy([cell, Def("root", "", ("a", "b"), (0, 1), (0, 1), (("cell", (0, 0), None),))], "root")
    with pytest.raises(ValueError):
        Hierarchy([Def("cx", "", ("a", "b"), (0, 1), (0, 1), ())], "cx")


def test_benchmarks_are_there():
    assert BENCHMARKS, "no hierarchical benchmarks in benchmarks/qualtran"


@pytest.mark.parametrize("path", BENCHMARKS, ids=lambda p: p.stem)
def test_benchmark_tree_partitions_its_problem(path):
    h = Hierarchy.load(path)
    assert path.with_suffix(".qasm").read_text() == h.to_qasm()
    for node in h.tree.walk():
        d = h.defs[node.name]
        assert len(node.qubits) == len(d.qubits) == len(set(node.qubits))
        assert set(d.ins) | set(d.outs) | set(d.workspace) == set(range(len(d.qubits)))
        assert all(set(h.problem.pairs[i].qubits) <= set(node.qubits) for i in node.pairs)
        at = node.start
        for child in node.children:   # in order, disjoint, inside the parent
            assert at <= child.start <= child.stop <= node.stop
            assert set(child.qubits) <= set(node.qubits)
            at = child.stop
    counts = h.counts(h.root)
    assert len(h.problem.pairs) == counts.get("cx", 0) + counts.get("t", 0) + counts.get("tdg", 0)


def test_a_benchmark_compiles_and_verifies():
    h = Hierarchy.load(ROOT / "benchmarks" / "qualtran" / "add-4.json")
    program = make("layout").solve(h.problem)
    assert verify(h.problem, program).steps >= h.problem.depth
    assert len(read_qasm(ROOT / "benchmarks" / "qualtran" / "add-4.qasm").pairs) == len(h.problem.pairs)


# ---- the generator: every circuit below is checked against Qualtran's own semantics ----

def _circuit(h):
    """The flat gates as a cirq circuit, measurements and conditions as written."""
    import cirq
    ops = {"h": cirq.H, "x": cirq.X, "s": cirq.S, "sdg": cirq.S ** -1, "t": cirq.T, "tdg": cirq.T ** -1, "cx": cirq.CNOT,
           "reset": cirq.ResetChannel()}
    qs = cirq.LineQubit.range(h.n_qubits)
    out, key = [], {}
    for k, (g, ids, cond) in enumerate(h.gates):
        if g == "measure":
            key[ids[0]] = f"m{k}"
            out.append(cirq.measure(qs[ids[0]], key=key[ids[0]]))
            continue
        op = ops[g].on(*(qs[i] for i in ids))
        out.append(op if cond is None else op.with_classical_controls(key[cond]))
    return cirq.Circuit(out), qs


def _bits(regs, values):
    out = []
    for reg, value in zip(regs, values):
        for item in ([value] if not reg.shape else [value[idx] for idx in reg.all_idxs()]):
            out += [int(b) for b in reg.dtype.to_bits(item)]
    return out


def _classical_cases():
    import numpy as np
    from qualtran import QUInt
    from qualtran.bloqs.arithmetic import Add, LessThanEqual
    from qualtran.bloqs.basic_gates import CSwap
    from qualtran.bloqs.data_loading.qrom import QROM
    from qualtran.bloqs.gf_arithmetic import GF2Inverse, GF2Multiplication
    from qualtran.bloqs.mcmt import MultiAnd
    from qualtran.bloqs.mod_arithmetic import ModAdd
    rng = np.random.default_rng(1)

    def ints(names, hi, k=5):
        return [{name: int(rng.integers(0, hi)) for name in names} for _ in range(k)]
    mul, inv = GF2Multiplication(3), GF2Inverse(3)
    return [
        (Add(QUInt(4)), ints("ab", 16)),
        (MultiAnd((1, 0, 1)), [{"ctrl": np.array(v)} for v in ([1, 0, 1], [1, 1, 1], [0, 0, 0])]),
        (CSwap(2), [{"ctrl": c, "x": x, "y": y} for c, x, y in ((1, 2, 1), (0, 2, 1), (1, 3, 0))]),
        (ModAdd(3, 7), ints("xy", 7)),
        (LessThanEqual(3, 3), [dict(s, target=0) for s in ints("xy", 8)]),
        (mul, [{"x": mul.qgf.gf_type(int(a)), "y": mul.qgf.gf_type(int(b))} for a, b in rng.integers(0, 8, size=(5, 2))]),
        (inv, [{"x": inv.qgf.gf_type(a)} for a in (1, 2, 5)]),
        (QROM.build_from_data(np.random.default_rng(0).integers(0, 8, size=8)), [{"selection": s, "target0_": 0} for s in range(8)]),
    ]


@needs_qualtran
@pytest.mark.parametrize("case", range(8))
def test_generated_circuit_computes_what_the_bloq_computes(case):
    """Basis states in, basis states out: the outputs Qualtran's classical simulation gives,
    and every other qubit back at 0, so lent workspace really comes back clean."""
    import cirq
    import numpy as np
    bloq, samples = _classical_cases()[case]
    h = qualtran_bloqs.hierarchy(bloq)
    root = h.defs[h.root]
    circuit, qs = _circuit(h)
    lefts, rights = list(bloq.signature.lefts()), list(bloq.signature.rights())
    sim = cirq.Simulator(seed=0, dtype=np.complex64)
    for sample in samples:
        start, want = [0] * h.n_qubits, [0] * h.n_qubits
        for i, b in zip(root.ins, _bits(lefts, [sample[r.name] for r in lefts])):
            start[i] = b
        for i, b in zip(root.outs, _bits(rights, bloq.call_classically(**sample))):
            want[i] = b
        state = sim.simulate(circuit, qubit_order=qs, initial_state=int("".join(map(str, start)), 2)).final_state_vector
        k = int(np.argmax(np.abs(state)))
        assert abs(abs(state[k]) - 1) < 1e-3
        assert [int(b) for b in format(k, f"0{h.n_qubits}b")] == want, sample


@needs_qualtran
def test_translated_gates_keep_the_unitary_up_to_phase():
    import cirq
    import numpy as np
    a, b, c = cirq.LineQubit.range(3)
    ops = [cirq.Y(a), cirq.Z(a), (cirq.X ** 0.5)(a), (cirq.X ** -0.5)(a), (cirq.Y ** 0.5)(a), (cirq.Y ** -0.25)(a),
           (cirq.Z ** 0.75)(a), (cirq.Z ** 1.25)(a), cirq.CZ(a, b), cirq.Y(b).controlled_by(a), cirq.CCX(a, b, c),
           cirq.CCZ(a, b, c), cirq.CSWAP(a, b, c)]
    gate = {"h": cirq.H, "x": cirq.X, "s": cirq.S, "sdg": cirq.S ** -1, "t": cirq.T, "tdg": cirq.T ** -1, "cx": cirq.CNOT}
    for op in ops:
        made = cirq.Circuit(gate[g].on(*(cirq.LineQubit(i) for i in ids)) for g, ids, _ in qualtran_bloqs.gates_of(op, qualtran_bloqs.Pool()))
        want = cirq.unitary(cirq.Circuit(op))
        got = made.unitary(qubit_order=sorted(op.qubits))
        assert cirq.allclose_up_to_global_phase(got, want), op
    assert qualtran_bloqs.translate(cirq.rz(0.3)(a)) is None


@needs_qualtran
def test_linear_circuit_applies_its_matrix():
    import numpy as np
    rng = np.random.default_rng(0)
    for n in (1, 3, 6):
        while True:
            m = rng.integers(0, 2, size=(n, n))
            if round(abs(np.linalg.det(m))) % 2:
                break
        for _ in range(4):
            x = rng.integers(0, 2, size=n)
            y = x.copy()
            for control, target in qualtran_bloqs.linear(m):
                y[target] ^= y[control]
            assert (y == m @ x % 2).all()


@needs_qualtran
def test_t_count_matches_qualtran():
    """4 T per And, 7 per Toffoli and CSwap, on top of the T gates Qualtran counts itself."""
    from qualtran.resource_counting import QECGatesCost, get_cost_value
    for name in ("add-8", "modadd-4", "gf2mul-4", "modmul-4", "hubbard-2"):
        family, _, size = name.rpartition("-")
        bloq = qualtran_bloqs.FAMILIES[family][0](int(size))
        cost = get_cost_value(bloq, QECGatesCost())
        h = qualtran_bloqs.hierarchy(bloq)
        got = h.counts(h.root)
        assert got.get("t", 0) + got.get("tdg", 0) == cost.t + 4 * cost.and_bloq + 7 * (cost.toffoli + cost.cswap), name


@needs_qualtran
def test_committed_benchmark_is_what_the_generator_writes():
    for name in ("add-4", "gf2mul-4"):
        h = qualtran_bloqs.build(name)
        saved = json.loads((ROOT / "benchmarks" / "qualtran" / f"{name}.json").read_text())
        assert saved["defs"] == [d.to_dict() for d in h.defs.values()] and saved["root"] == h.root
