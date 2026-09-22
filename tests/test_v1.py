import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from miniflash import (MAGIC, Mapping, Pair, Problem, Program, Compiler, Route, Segment, VerifyError,
                       check_route, make, read_qasm, to_dot, verify, write_gltf)
from miniflash.program import MAGIC as MAGIC_KIND, QUBIT, ROUTE, Step
from miniflash.route import RouteError

ROOT = Path(__file__).resolve().parents[1]


def t_only(n, count):
    """`count` T gates round-robin over n qubits: layer demand with no CX at all."""
    b = Problem.builder(n)
    for i in range(count):
        b.add_t(i % n)
    return b.build()


def small_problem():
    return Problem.from_pairs(4, [Pair(0, 1, "cx"), Pair(1, MAGIC, "t"), Pair(2, 3, "cx"), Pair(3, 0, "cx")])


def test_problem_dag_is_program_order_per_qubit():
    p = small_problem()
    assert p.preds == ((), (0,), (), (0, 2))
    assert p.layers() == [[0, 2], [1, 3]]
    assert p.depth == 2
    assert p.criticality() == [2, 1, 2, 1]
    assert p.succs() == ((1, 3), (), (3,), ())


def test_builder_matches_from_pairs():
    built = Problem.builder(4).add_cx(0, 1).add_t(1).add_cx(2, 3).add_cx(3, 0).build()
    assert built == small_problem()
    assert Problem.builder(2).add_tdg(1).build().pairs == (Pair(1, MAGIC, "tdg"),)
    with pytest.raises(ValueError):
        Problem.builder(2).add_cx(0, 5).build()


def test_problem_rejects_bad_pairs():
    with pytest.raises(ValueError):
        Problem.from_pairs(2, [Pair(0, 0, "cx")])
    with pytest.raises(ValueError):
        Problem.from_pairs(2, [Pair(0, 2, "cx")])
    with pytest.raises(ValueError):
        Problem.from_pairs(2, [Pair(0, 1, "t")])


def test_read_qasm_keeps_idle_qubits_and_counts_cliffords(tmp_path):
    f = tmp_path / "c.qasm"
    f.write_text("OPENQASM 2.0;\ninclude \"qelib1.inc\";\nqreg q[3];\nh q[0];\ncx q[0],q[1];\ntdg q[1]; // tail\n")
    p = read_qasm(f)
    assert p.n_qubits == 3
    assert p.pairs == (Pair(0, 1, "cx"), Pair(1, MAGIC, "tdg"))
    assert p.dropped == {"h": 1}
    src = to_dot(p)
    assert "p0 -> p1" in src and "q1" in src


def test_read_qasm_expands_cliffords_and_drops_them(tmp_path):
    f = tmp_path / "c.qasm"
    f.write_text('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\ncz q[0],q[1];\nh q[0];\nt q[1];\n')
    p = read_qasm(f)
    assert [(pr.z, pr.x, pr.tag) for pr in p.pairs] == [(0, 1, "cx"), (1, MAGIC, "t")]
    assert p.dropped["h"] == 3


def test_read_qasm_rejects_unknown_gate(tmp_path):
    f = tmp_path / "c.qasm"
    f.write_text('OPENQASM 2.0;\ninclude "qelib1.inc";\nqreg q[2];\nrz(0.3) q[0];\n')
    with pytest.raises(ValueError):
        read_qasm(f)


def test_sparse_array_matches_dascot_layout():
    m = Mapping.sparse(2, 2, 3)
    assert (m.height, m.width) == (7, 7)
    assert m.qubits == {0: (2, 2), 1: (2, 4), 2: (4, 2)}
    assert set(m.magic) == {(0, 1), (0, 3), (0, 5), (1, 6), (3, 6), (5, 6), (6, 5), (6, 3), (6, 1), (5, 0), (3, 0), (1, 0)}
    assert m.is_free((3, 3)) and not m.is_free((2, 2)) and not m.is_free((0, 1))


def test_route_rules():
    m = Mapping.sparse(2, 2, 4)  # q0 (2,2), q1 (2,4), q2 (4,2), q3 (4,4)
    ok = Route(0, [Segment((3, 2), (3, 3)), Segment((3, 3), (4, 3))])
    check_route(ok, Pair(0, 3, "cx"), m)
    assert ok.tiles() == [(3, 2), (3, 3), (4, 3)]
    with pytest.raises(RouteError):
        Segment((0, 0), (1, 1))  # not straight
    with pytest.raises(RouteError):
        Route(0, [Segment((3, 2), (3, 3)), Segment((3, 4), (4, 4))])  # not chained
    with pytest.raises(RouteError):
        check_route(Route(0, [Segment((2, 3), (2, 3))]), Pair(0, 1, "cx"), m)  # starts beside, not above
    with pytest.raises(RouteError):
        check_route(Route(0, [Segment((3, 2), (3, 2))]), Pair(0, 2, "cx"), m)  # ends above target
    with pytest.raises(RouteError):
        check_route(Route(1, [Segment((3, 4), (3, 5))]), Pair(1, MAGIC, "t"), m)  # no magic named
    check_route(Route(1, [Segment((3, 4), (3, 5))], magic=(3, 6)), Pair(1, MAGIC, "t"), m)


def solved_small():
    p = small_problem()
    prog = Compiler(dependency=False).solve(p)
    return p, prog


def test_naive_solver_one_pair_per_step_and_verifies():
    p, prog = solved_small()
    assert len(prog) == len(p.pairs)
    rep = verify(p, prog)
    assert rep.volume == prog.volume == 7 * 7 * 4
    assert rep.walk_steps == 0
    assert rep.when == {0: 0, 1: 1, 2: 2, 3: 3}
    t_step = prog.steps[1]
    consumed = [v for v in t_step.of_kind(MAGIC_KIND) if v.id != -1]
    assert len(consumed) == 1 and consumed[0].id == 1


def test_program_json_roundtrip(tmp_path):
    p, prog = solved_small()
    prog.save(tmp_path / "p.json")
    back = Program.load(tmp_path / "p.json")
    assert back.to_dict() == prog.to_dict()
    verify(p, back)


def test_verify_rejects_tampering():
    p, prog = solved_small()
    # drop the last step: pair 3 never runs
    with pytest.raises(VerifyError, match="never executed"):
        verify(p, Program(prog.height, prog.width, prog.steps[:-1]))
    # swap two steps: pair 1 before its predecessor
    with pytest.raises(VerifyError, match="predecessor"):
        verify(p, Program(prog.height, prog.width, [prog.steps[1], prog.steps[0]] + prog.steps[2:]))
    # relabel a route tile with a different pair id
    s = Step.from_dict(prog.steps[0].to_dict())
    tile = next(v.tile for v in s.of_kind(ROUTE))
    s.vertices[tile] = type(s.vertices[tile])(tile, ROUTE, 2)
    with pytest.raises(VerifyError, match="pair ids"):
        verify(p, Program(prog.height, prog.width, [s] + prog.steps[1:]))
    # teleport a qubit
    s = Step.from_dict(prog.steps[1].to_dict())
    q0 = s.vertices.pop((2, 2))
    s.vertices[(6, 6)] = type(q0)((6, 6), QUBIT, 0)
    with pytest.raises(VerifyError, match="jumps"):
        verify(p, Program(prog.height, prog.width, [prog.steps[0], s] + prog.steps[2:]))


def test_walk_step_is_accepted_and_counted():
    p = Problem.from_pairs(2, [Pair(0, 1, "cx")])
    m = Mapping.sparse(2, 2, 2)
    prog = Compiler(dependency=False).solve(p)
    walk = Step()
    walk.add((3, 2), QUBIT, 0)  # q0 moved one tile down
    walk.add((2, 4), QUBIT, 1)
    for mg in m.magic:
        walk.add(mg, MAGIC_KIND, -1)
    prog.steps.append(walk)
    rep = verify(p, prog)
    assert rep.walk_steps == 1 and rep.steps == 2


def test_benchmark_circuit_end_to_end(tmp_path):
    p = read_qasm(ROOT / "benchmarks" / "toffoli" / "barenco-tof-3.qasm")
    assert p.n_qubits == 5 and len(p.pairs) == 52
    prog = Compiler(dependency=False).solve(p)
    rep = verify(p, prog)
    assert rep.steps == 52 and (rep.height, rep.width) == (9, 9)


def test_asap_solver_parallelises_and_verifies():
    p = read_qasm(ROOT / "benchmarks" / "toffoli" / "barenco-tof-3.qasm")
    prog = Compiler().solve(p)
    rep = verify(p, prog)
    assert rep.steps == p.depth  # nothing blocks on this small layout: schedule meets the dependency depth
    assert rep.steps < len(p.pairs)
    assert max(len(s.components()) for s in prog.steps) >= 2
    # the same pair set as the naive program, in dependency order
    assert set(rep.when) == set(range(len(p.pairs)))


def test_asap_keeps_blocked_pairs_in_the_frontier():
    # two independent T gates on q0 and q1 compete for magic tiles; two independent CXs share a lane
    p = Problem.from_pairs(4, [Pair(0, MAGIC, "t"), Pair(1, MAGIC, "t"), Pair(2, 3, "cx"), Pair(3, 2, "cx")])
    prog = Compiler().solve(p)
    rep = verify(p, prog)
    assert rep.when[0] == rep.when[1] == 0          # different magic tiles, same step
    assert rep.when[3] == rep.when[2] + 1           # dependency, not contention


def test_compact_drops_unused_rows_and_cols_losslessly():
    p = read_qasm(ROOT / "benchmarks" / "algorithms" / "bv-16.qasm")
    plain = verify(p, Compiler().solve(p))
    s = Compiler(spacing="whole")
    rep = verify(p, s.solve(p))
    assert rep.steps == plain.steps                       # same schedule, only the grid shrinks
    assert (rep.height, rep.width) < (plain.height, plain.width)
    assert rep.height == len(s.keep_rows) and rep.width == len(s.keep_cols)
    assert rep.volume < plain.volume
    # only consumed magic tiles survive, and each of them is consumed somewhere
    from miniflash.program import MAGIC as MAGIC_KIND
    prog = s.solve(p)
    magic_tiles = {v.tile for v in prog.steps[0].of_kind(MAGIC_KIND)}
    consumed = {v.tile for st in prog.steps for v in st.of_kind(MAGIC_KIND) if v.id != -1}
    assert magic_tiles == consumed and len(magic_tiles) < len(Mapping.sparse(4, 4, p.n_qubits).magic)


def test_compact_keeps_crossed_rows_out_and_endpoints_in():
    from miniflash import compact
    from miniflash.route import Segment
    # q0 at (2,2), q1 at (6,2) on a 9x5 grid with no magic: a vertical route crosses rows 3..5
    m = Mapping(9, 5, {0: (2, 2), 1: (6, 2)}, ())
    r = Route(0, [Segment((3, 2), (3, 3)), Segment((3, 3), (6, 3))])
    p = Problem.from_pairs(2, [Pair(0, 1, "cx")])
    check_route(r, p.pairs[0], m)
    m2, sched2, keep_r, keep_c = compact(m, [[r]])
    assert keep_r == [2, 3, 6] and keep_c == [2, 3]     # rows 4, 5 only crossed -> gone; cols 0, 1, 4 unused
    assert (m2.height, m2.width) == (3, 2)
    verify(p, Program.from_routes(p, m2, sched2))


def test_rearrange_partitions_walks_and_verifies():
    p = read_qasm(ROOT / "benchmarks" / "toffoli" / "barenco-tof-3.qasm")
    s = Compiler(spacing=True, row_budget=4, col_budget=6)
    prog = s.solve(p)
    rep = verify(p, prog)
    assert len(s.partitions) > 1
    assert s.partitions[0].start == 0 and s.partitions[-1].stop == p.depth
    assert all(a.stop == b.start for a, b in zip(s.partitions, s.partitions[1:]))
    # a partition of several steps respects the budget; a single step may exceed it on its own
    assert all(q.mapping.height <= 4 and q.mapping.width <= 6 for q in s.partitions if q.stop - q.start > 1)
    assert rep.height == max(q.mapping.height for q in s.partitions)
    assert rep.width == max(q.mapping.width for q in s.partitions)
    assert rep.walk_steps > 0 and rep.steps == p.depth + rep.walk_steps
    assert any(st.is_walk for st in prog.steps)


def test_walk_steps_move_one_tile_per_axis_without_collision():
    from miniflash.solver.spacing import walk_steps
    a = Mapping(6, 6, {0: (0, 0), 1: (0, 2), 2: (2, 0), 3: (2, 2)})
    b = Mapping(6, 6, {0: (0, 0), 1: (0, 1), 2: (3, 0), 3: (3, 4)})
    steps = walk_steps(a, b)
    assert len(steps) == 2                                         # max Chebyshev displacement
    assert steps[-1].qubit_tiles() == b.qubits
    prev = a.qubits
    for st in steps:
        cur = st.qubit_tiles()
        assert len(set(cur.values())) == 4
        assert all(max(abs(cur[q][0] - prev[q][0]), abs(cur[q][1] - prev[q][1])) <= 1 for q in cur)
        prev = cur


def test_sharelane_respects_budget_and_verifies():
    p = read_qasm(ROOT / "benchmarks" / "arithmetic" / "mod5-4.qasm")
    s = Compiler(routing=True, spacing=True, extra=4, share=4.0)
    rep = verify(p, s.solve(p))
    k = s.layout.site_rows
    # every multi-step partition fits the budget, and the chip does too
    assert all(q.mapping.height <= k + 4 and q.mapping.width <= k + 4 for q in s.partitions if q.stop - q.start > 1)
    assert rep.height <= k + 4 and rep.width <= k + 4
    assert s.partitions[0].start == 0 and s.partitions[-1].stop == rep.steps - rep.walk_steps
    assert all(a.stop == b.start for a, b in zip(s.partitions, s.partitions[1:]))


def test_sharelane_penalty_prefers_loaded_lanes():
    from miniflash.solver.routing import lines_of
    # two CX pairs in one step whose candidates can share a lane row: with a penalty the second
    # route reuses the first one's row instead of opening a fresh one
    p = Problem.from_pairs(8, [Pair(0, 2, "cx"), Pair(1, 3, "cx")])   # k=3: q0 (2,2) q1 (2,4) q2 (2,6) q3 (4,2)
    shared = Compiler(routing=True, spacing=True, extra=100, share=8.0)
    shared.solve(p)
    r0, r1 = shared.partitions[0].schedule[0]
    rows0, _ = lines_of(r0)
    rows1, _ = lines_of(r1)
    plain = Compiler(routing=True, spacing=True, extra=100, share=0.0)
    plain.solve(p)
    q0, q1 = plain.partitions[0].schedule[0]
    assert r0.pair == 0 and r1.pair == 1 and q1.length <= r1.length          # the penalty may lengthen a route
    assert len(rows0 | rows1) <= len(set(lines_of(q0)[0]) | set(lines_of(q1)[0]))  # but never loads more rows


def test_interaction_graph_counts_cx_only():
    p = Problem.from_pairs(4, [Pair(0, 1, "cx"), Pair(1, 0, "cx"), Pair(2, 3, "cx"), Pair(1, MAGIC, "t")])
    assert p.interaction() == {(0, 1): 2, (2, 3): 1}


def test_spectral_seed_places_interacting_qubits_together():
    from miniflash.solver.placement import spectral_order
    # two tight cliques {0,1,2,3} and {4,5,6,7}: each must occupy a compact half of the 3x3 site array
    pairs = [Pair(a, b, "cx") for a in range(4) for b in range(4) if a != b] + \
            [Pair(a, b, "cx") for a in range(4, 8) for b in range(4, 8) if a != b]
    p = Problem.from_pairs(8, pairs)
    order = spectral_order(p, 3, 3)
    assert len(order) == 8 and len(set(order.values())) == 8
    def spread(qs):
        rows = [order[q][0] for q in qs]
        return max(rows) - min(rows)
    assert spread(range(4)) <= 1 and spread(range(4, 8)) <= 1
    assert {order[q][0] for q in range(4)}.isdisjoint({order[q][0] for q in range(4, 8)}) or \
        len({order[q][0] for q in range(8)}) == 3


def test_mapping_solver_lowers_proxy_and_verifies():
    p = read_qasm(ROOT / "benchmarks" / "toffoli" / "barenco-tof-5.qasm")
    m = Compiler(placement=True, seed=0)
    rep = verify(p, m.solve(p))
    assert m.final_cost <= m.initial_cost
    assert m.layout is not None and set(m.layout.order) == set(p.qubits)
    assert rep.steps >= p.depth
    # the same seed reproduces the placement; the placed order reaches the rearrangement stage
    again = Compiler(placement=True, seed=0)
    again.solve(p)
    assert again.layout.order == m.layout.order
    full = Compiler(routing=True, spacing=True, placement=True, seed=0)
    rep2 = verify(p, full.solve(p))
    assert full.layout.order == m.layout.order
    assert rep2.walk_steps >= 0


def test_sparse_rectangle_and_candidate_shapes():
    from miniflash import candidate_shapes
    m = Mapping.sparse(2, 3, 5)
    assert (m.height, m.width) == (7, 9)
    assert m.qubits == {0: (2, 2), 1: (2, 4), 2: (2, 6), 3: (4, 2), 4: (4, 4)}
    assert all(t[0] in (0, 6) or t[1] in (0, 8) for t in m.magic)
    with pytest.raises(ValueError):
        Mapping.sparse(1, 2, 5)
    assert candidate_shapes(24, 3) == [(3, 8), (1, 24), (2, 12)]          # paired-lane grid areas, main's order
    assert candidate_shapes(24, 3, squarest=True) == [(3, 8), (1, 24), (2, 12), (5, 5)]
    assert candidate_shapes(16, 3, squarest=True)[-1] == (4, 4)

    assert all(a <= b and a * b >= 16 for a, b in candidate_shapes(16))


def test_aspect_solver_keeps_cheapest_shape():
    p = t_only(8, 30)
    s = Compiler(routing=True, spacing=True, layout=3, exact=0)
    rep = verify(p, s.solve(p))
    assert 3 <= len(s.tried) <= 4 and s.shape in {t["shape"] for t in s.tried}
    assert rep.volume == min(t["volume"] for t in s.tried)
    assert (s.layout.site_rows, s.layout.site_cols) == s.shape






def test_exact_selection_orders_by_criticality_and_verifies():
    b = Problem.builder(6)
    b.add_cx(0, 5)                     # leaf
    b.add_cx(1, 4)                     # heads a chain of four T gates
    for _ in range(4):
        b.add_t(4)
    p = b.build()
    crit = p.criticality()
    assert crit[1] > crit[0] and crit[1] == 5
    s = Compiler(routing=True, spacing=True, placement=True, shape=(1, 6), anneal=False, exact=12)
    rep = verify(p, s.solve(p))
    assert rep.when[1] <= rep.when[0]                  # the critical pair is never delayed behind the leaf
    g = Compiler(routing=True, spacing=True, placement=True, shape=(1, 6), anneal=False, exact=0)
    verify(p, g.solve(p))                              # greedy path still verifies
    # the search over a front is exhaustive: on a paired array every pair has one or two candidates
    p2 = read_qasm(ROOT / "benchmarks" / "toffoli" / "barenco-tof-3.qasm")
    e = Compiler(routing=True, spacing=True, placement=True, seed=0, exact=12)
    verify(p2, e.solve(p2))


def test_every_qubit_faces_the_same_way():
    from miniflash.route import Segment
    # paired 1x4 array: q0 (1,2) q1 (1,3) share lane row 0; lane columns 1 and 4
    m = Mapping.paired(1, 4, 4)
    p = Problem.from_pairs(4, [Pair(0, 1, "cx")])
    # L from above q0 along row 0 to column 4, down to (1,4) beside q1's X side
    l_route = Route(0, [Segment((0, 2), (0, 4)), Segment((0, 4), (1, 4))])
    check_route(l_route, p.pairs[0], m)
    verify(p, Program.from_routes(p, m, [[l_route]]))
    # the straight lane-row segment ends on q1's Z side (above it), not its X side
    straight = Route(0, [Segment((0, 2), (0, 3))])
    with pytest.raises(RouteError):
        check_route(straight, p.pairs[0], m)
    step = Step.from_dict(Program.from_routes(p, m, [[l_route]]).steps[0].to_dict())
    bad = Step()
    for v in step.vertices.values():
        if v.kind != ROUTE:
            bad.add(v.tile, v.kind, v.id)
    for t in ((0, 2), (0, 3)):
        bad.add(t, ROUTE, 0)
    bad.link((1, 2), (0, 2)); bad.link((0, 2), (0, 3)); bad.link((0, 3), (1, 3))
    with pytest.raises(VerifyError, match="wrong side"):
        verify(p, Program(m.height, m.width, [bad]))
    assert "flipped" not in Mapping.__dataclass_fields__


def test_factory_charge_sizes_and_prices_the_supply():
    from miniflash import Factory, charge
    p = read_qasm(ROOT / "benchmarks" / "toffoli" / "barenco-tof-3.qasm")
    prog = Compiler(routing=True, spacing=True, placement=True, layout="stats", factory=Factory(3, 3, 11)).solve(p)
    t_gates = sum(1 for pair in p.pairs if pair.is_magic)
    peak = charge(prog, Factory(3, 3, 11, "peak"))
    rate = charge(prog, Factory(3, 3, 11, "rate"))
    tiles = charge(prog, Factory(3, 3, 11, "tiles"))
    assert peak.states == rate.states == tiles.states == t_gates          # every T consumes exactly one state
    assert peak.factories == peak.peak and rate.factories >= peak.factories
    assert rate.factories == max(sum(peak.consumed[i:i + 11]) for i in range(len(peak.consumed)))
    assert peak.factory_volume == peak.factories * 9 * len(prog.steps)
    assert peak.total(prog.volume) == prog.volume + peak.factory_volume
    assert Factory.parse("3x3x11:rate") == Factory(3, 3, 11, "rate") and str(Factory.parse("2x4x7")) == "2x4x7:peak"
    with pytest.raises(ValueError):
        Factory(3, 3, 11, "count")


def test_factory_supply_paces_magic_states_and_is_verified():
    from miniflash import Factory, charge
    p = read_qasm(ROOT / "benchmarks" / "toffoli" / "barenco-tof-3.qasm")
    t_gates = sum(1 for pair in p.pairs if pair.is_magic)
    s = Compiler(routing=True, spacing=True, placement=True, layout="stats", factory=Factory(3, 3, 11), factories=2)
    prog = s.solve(p)
    rep = verify(p, prog, Factory(3, 3, 11), factories=2)
    assert s.name == "layout-3x3x11-f2"
    assert rep.steps - rep.walk_steps >= t_gates * 11 // 2          # the last state arrives no earlier than k x period / F
    with pytest.raises(VerifyError):
        verify(p, prog, Factory(3, 3, 11), factories=1)                       # one factory could not have supplied it
    loose = Compiler(routing=True, spacing=True, placement=True, layout="stats", factory=Factory(3, 3, 11), factories=40).solve(p)
    assert len(loose.steps) < len(prog.steps)
    bill = charge(prog, Factory(3, 3, 11), factories=2)
    assert bill.factories == 2 and bill.factory_volume == 2 * 9 * len(prog.steps) and bill.states == t_gates


def test_gltf_renders_merges_walks_and_magic(tmp_path):
    import json
    from miniflash.gltf import STRIDE, lower
    p = read_qasm(ROOT / "benchmarks" / "toffoli" / "barenco-tof-3.qasm")
    prog = make("placement", seed=0).solve(p)
    rep = verify(p, prog)
    pipes, cubes, magic = lower(prog)
    n_cx = sum(not pr.is_magic for pr in p.pairs)
    assert len(magic) == len(p.pairs) - n_cx                       # one green box per consumed magic state
    k_pipes = [pp for pp in pipes if pp.axis == "K"]
    # each qubit spans every layer (two per step), plus one ancilla pipe per CX merge
    assert len(k_pipes) == p.n_qubits * (2 * rep.steps - 1) + n_cx
    assert max(pp.hi[2] for pp in pipes) == STRIDE * (2 * rep.steps - 1)
    # Z halves (blue) sit on the lower layer of a step, X halves (red) on the upper one
    for pp in pipes:
        if pp.axis != "K" and pp.parity == 1:
            assert (pp.lo[2] // STRIDE) % 2 == 0
    out = tmp_path / "tof3.gltf"
    write_gltf(prog, out)
    scene = json.loads(out.read_text())
    assert len(scene["nodes"]) == len(pipes) + len(cubes) + len(magic) + 1


def test_gltf_single_cnot_splits_at_the_turn():
    from miniflash.gltf import STRIDE, lower
    p = Problem.from_pairs(2, [Pair(0, 1, "cx")])
    prog = make("placement").solve(p)
    verify(p, prog)
    pipes, _, _ = lower(prog)
    ancillas = [pp for pp in pipes if pp.axis == "K" and pp.parity == 1]
    assert len(ancillas) == 1
    (a,) = ancillas
    assert (a.lo[2], a.hi[2]) == (0, STRIDE)                       # climbs from layer 0 to layer 1
    lower_half = [pp for pp in pipes if pp.axis != "K" and pp.lo[2] == 0]
    upper_half = [pp for pp in pipes if pp.axis != "K" and pp.lo[2] == STRIDE]
    assert lower_half and upper_half
    assert all(pp.parity == 1 for pp in lower_half) and all(pp.parity == 0 for pp in upper_half)
    # the lower half ends at the ancilla, where the upper half starts
    assert a.lo in {pp.hi for pp in lower_half} | {pp.lo for pp in lower_half}
    assert a.hi in {pp.hi for pp in upper_half} | {pp.lo for pp in upper_half}


def test_supply_rings_factories_and_deliveries(tmp_path):
    import json
    from miniflash import Factory, plan
    from miniflash.gltf import factory_boxes, lower
    from miniflash.supply import place_factories, ring_tiles
    # two factories on a 2 x 9 chip: two rings, both boxes on the top side, ports facing the rings
    boxes = place_factories(2, 2, 9, Factory())
    assert [(b.side, b.row, b.col, b.port) for b in boxes] == [("top", -5, -2, (-3, -1)), ("top", -5, 2, (-3, 3))]
    assert len(ring_tiles(1, 2, 9)) == 2 * (4 + 11) - 4
    assert sorted(b.ring for b in boxes) == [1, 2] and boxes[0].ring == 2      # the corner-most port takes the outer ring
    # four factories on a 3 x 5 chip: the first port lies beyond ring 1's ends, so it takes ring 4
    four = place_factories(4, 3, 5, Factory())
    assert sorted(b.ring for b in four) == [1, 2, 3, 4] and four[0].ring == 4
    p = read_qasm(ROOT / "benchmarks" / "toffoli" / "tof-3.qasm")
    for f in (1, 2):
        prog = make("layout", factories=f).solve(p)
        verify(p, prog, factories=f)
        sup = plan(prog, f)
        assert not sup.conflicts
        assert len(sup.deliveries) == sum(pr.is_magic for pr in p.pairs)
        for d in sup.deliveries:
            ring = sup.boxes[d.factory].ring
            assert d.tiles[0] == sup.boxes[d.factory].port and d.tiles[-1] == d.magic
            assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip(d.tiles, d.tiles[1:]))
            assert set(ring_tiles(ring, prog.height, prog.width)) & set(d.tiles)          # it runs on its own ring
            for other in range(1, f + 1):
                if other != ring:                                                       # other rings are only crossed
                    assert len(set(ring_tiles(other, prog.height, prog.width)) & set(d.tiles)) <= 1
        out = tmp_path / f"tof3-f{f}.gltf"
        write_gltf(prog, out, sup)
        pipes, cubes, magic = lower(prog, sup)
        assert len(json.loads(out.read_text())["nodes"]) == len(pipes) + len(cubes) + len(magic) + f + 1
        assert len(factory_boxes(prog, sup)) == f
