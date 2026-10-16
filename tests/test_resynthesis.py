import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from miniflash import MAGIC, Pair, Problem, Program, make, read_qasm, verify
from miniflash.program import MAGIC as MAGIC_KIND, QUBIT, ROUTE, Step
from miniflash.resynthesis import Box, Budget, Component, Window, close, extract_greedy, slabs, stages

ROOT = Path(__file__).resolve().parents[1]

# 6x6 grid, qubits at (1,1) (1,3) (4,1) (4,3), a magic tile at (0,5)
SEATS = {0: (1, 1), 1: (1, 3), 2: (4, 1), 3: (4, 3)}
MAGIC_TILE = (0, 5)


def step_with(routes, consumed=None):
    """A step seating every qubit, plus one route per (pair, [tiles], z, x_or_magic)."""
    s = Step()
    for q, tile in SEATS.items():
        s.add(tile, QUBIT, q)
    s.add(MAGIC_TILE, MAGIC_KIND, consumed if consumed is not None else -1)
    for pair, tiles, z, x in routes:
        for t in tiles:
            s.add(t, ROUTE, pair)
        s.link(SEATS[z], tiles[0])
        for a, b in zip(tiles, tiles[1:]):
            s.link(a, b)
        s.link(tiles[-1], SEATS[x] if x is not None else MAGIC_TILE)
    return s


CX01 = (0, [(0, 1), (0, 2), (1, 2)], 0, 1)          # q0 Z side above, into q1's left side
CX23 = (1, [(3, 1), (3, 2), (4, 2)], 2, 3)          # same shape two rows down
CX12 = (2, [(2, 3), (3, 3), (3, 2), (4, 2)], 1, 2)  # q1 down to q2's right side, spans both


def two_far_apart():
    problem = Problem.from_pairs(4, [Pair(0, 1, "cx"), Pair(2, 3, "cx"), Pair(1, 2, "cx")])
    program = Program(6, 6, [step_with([CX01, CX23]), step_with([CX12])])
    verify(problem, program)
    return problem, program


def test_components_of_step_carry_pair_tiles_and_parties():
    _, program = two_far_apart()
    comps = Component.of_step(program.steps[0], 0)
    assert [c.pair for c in comps] == [0, 1]
    assert comps[0].qubits == (0, 1) and comps[0].magic is None
    assert comps[0].tiles == frozenset({(1, 1), (0, 1), (0, 2), (1, 2), (1, 3)})
    assert comps[0].box == Box(0, 1, 1, 3)
    assert comps[1].box == Box(3, 1, 4, 3)


def test_disjoint_components_give_disjoint_closed_windows():
    problem, program = two_far_apart()
    ws = close(program, 0, 1)
    assert [w.box for w in ws] == [Box(0, 1, 1, 3), Box(3, 1, 4, 3)]
    assert ws[0].pairs == (0,) and ws[0].participants == (0, 1)
    assert ws[0].bystanders == {} and ws[0].idle_magic == ()
    assert ws[0].ports == 4 and ws[0].cubes == 2 * 3 * 2
    for w in ws:
        w.check(program)


def test_overlap_merges_transitively_into_one_window():
    problem, program = two_far_apart()
    (w,) = close(program, 0, 2)
    assert w.box == Box(0, 1, 4, 3)
    assert w.pairs == (0, 1, 2) and w.participants == (0, 1, 2, 3)
    assert w.critical_path(problem) == 2 and w.slack(problem) == 0
    w.check(program)
    # a window that claims the merged box over one step is not closed: CX12 is missing
    with pytest.raises(ValueError):
        Window(Box(0, 1, 4, 3), 1, 2, (), {}, ()).check(program)
    # and the second step alone still lists q0 and q3 as bystanders
    (w1,) = close(program, 1, 2)
    assert w1.participants == (1, 2) and w1.bystanders == {0: (1, 1), 3: (4, 3)}


def test_magic_consumption_is_a_port_not_a_bystander():
    problem = Problem.from_pairs(4, [Pair(1, MAGIC, "t")])
    t_route = (0, [(0, 3), (0, 4)], 1, None)
    program = Program(6, 6, [step_with([t_route], consumed=0)])
    verify(problem, program)
    (w,) = close(program, 0, 1)
    assert w.magic == ((0, MAGIC_TILE),) and w.ports == 3
    assert w.idle_magic == () and w.box == Box(0, 3, 1, 5)
    # an idle magic tile inside a box is listed, not counted as a port
    problem2, program2 = two_far_apart()
    wide = Window(Box(0, 1, 1, 5), 0, 1, close(program2, 0, 1)[0].components, {}, (MAGIC_TILE,))
    assert wide.idle_magic == (MAGIC_TILE,) and wide.ports == 4


def test_stages_split_at_walk_steps():
    problem, program = two_far_apart()
    walk = Step()
    for q, tile in SEATS.items():
        walk.add(tile, QUBIT, q)
    program.steps.insert(1, walk)
    assert stages(program) == [(0, 1), (2, 3)]
    assert all(s.stop - s.start == 1 for s in slabs(program))


def test_slab_gain_and_greedy_pick():
    # step 0: cx(0,1); step 1: cx(2,3), independent, so the two-step slab has slack 1
    problem = Problem.from_pairs(4, [Pair(0, 1, "cx"), Pair(2, 3, "cx")])
    program = Program(6, 6, [step_with([CX01]), step_with([CX23])])
    verify(problem, program)
    all_slabs = slabs(program)
    assert {(s.start, s.stop) for s in all_slabs} == {(0, 1), (1, 2), (0, 2)}
    two = next(s for s in all_slabs if (s.start, s.stop) == (0, 2))
    assert len(two.windows) == 2 and two.gain(problem) == 1
    assert [(s.start, s.stop) for s in extract_greedy(program, problem)] == [(0, 2)]
    assert extract_greedy(program, problem, min_gain=2) == []
    # a budget too small for a two-step window stops the range early
    assert {(s.start, s.stop) for s in slabs(program, Budget(cubes=12))} == {(0, 1), (1, 2)}


@pytest.mark.parametrize("name", ["toffoli/tof-3.qasm", "algorithms/ghz8.qasm"])
def test_extracted_windows_are_closed_and_cover_their_slabs(name):
    problem = read_qasm(ROOT / "benchmarks" / name)
    program = make("layout").solve(problem)
    verify(problem, program)
    picked = extract_greedy(program, problem, min_gain=0)
    assert picked
    for a, b in zip(picked, picked[1:]):
        assert a.stop <= b.start
    for slab in picked:
        boxes = [w.box for w in slab.windows]
        for i, x in enumerate(boxes):
            assert not any(x.overlaps(y) for y in boxes[i + 1:])
        for w in slab.windows:
            w.check(program)
            assert Budget().fits(w)
        covered = {(c.step, c.pair) for w in slab.windows for c in w.components}
        every = {(t, c.pair) for t in range(slab.start, slab.stop) for c in Component.of_step(program.steps[t], t)}
        assert covered == every


def test_gltf_overlays_and_carve(tmp_path):
    import json
    from miniflash.gltf import STRIDE, write_gltf
    from miniflash.resynthesis import boxes, window_box
    problem, program = two_far_apart()
    ws = close(program, 0, 1)
    lo, hi = window_box(ws[0])
    assert lo == (STRIDE * 1, STRIDE * 0, 0) and hi == (STRIDE * 3, STRIDE * 1, STRIDE * 1)
    from miniflash.resynthesis import Slab
    overlays = boxes([Slab(0, 1, tuple(ws))])
    assert len(overlays) == 2 and overlays[0][2][3] == 0.35
    plain, over, carved = tmp_path / "p.gltf", tmp_path / "o.gltf", tmp_path / "c.gltf"
    write_gltf(program, plain)
    write_gltf(program, over, overlays=overlays)
    write_gltf(program, carved, overlays=overlays, carve=True)
    p, o, c = (json.loads(f.read_text()) for f in (plain, over, carved))
    assert len(p["materials"]) == 6 and len(o["materials"]) == 7 and o["materials"][6]["alphaMode"] == "BLEND"
    assert len(o["nodes"]) == len(p["nodes"]) + 2
    box = [n for n in o["nodes"] if n.get("mesh") == 6][0]
    assert box["translation"] == [2.0, 0.0, 0.0] and box["scale"] == [5.0, 3.0, 3.0]
    # carving drops the merge pipes and cubes inside the boxes but keeps the qubit pipes leaving them
    assert len(c["nodes"]) < len(o["nodes"])
    assert any(n.get("mesh") == 6 for n in c["nodes"])


def test_extract_dp_prefers_balanced_slabs_and_never_skips_a_step_that_fits():
    from miniflash.resynthesis import Weights, cost, extract_dp, skip_cost, total
    # cx(0,1) then cx(2,3): independent, so one two-step slab has depth 1 and no holes
    problem = Problem.from_pairs(4, [Pair(0, 1, "cx"), Pair(2, 3, "cx")])
    program = Program(6, 6, [step_with([CX01]), step_with([CX23])])
    (slab,) = extract_dp(program, problem)
    assert (slab.start, slab.stop) == (0, 2)
    assert slab.depth(problem) == 1 and slab.holes(problem) == 0 and slab.cuts(problem) == 0
    assert cost(slab, problem, program.area) == 36
    # a chain in the merged window: depth 2, the far window one step shallower leaves holes
    problem2, program2 = two_far_apart()
    (one,) = [s for s in slabs(program2) if (s.start, s.stop) == (0, 2)]
    assert one.depth(problem2) == 2 and one.holes(problem2) == 0 and one.cuts(problem2) == 0
    picked = extract_dp(program2, problem2)
    assert [(s.start, s.stop) for s in picked] == [(0, 2)]          # cutting would sever two edges
    assert [(s.start, s.stop) for s in extract_dp(program2, problem2, weights=Weights(cuts=0.0))] in ([(0, 2)], [(0, 1), (1, 2)])
    # a budget no window of step 1 fits: that step is skipped and charged its severed edges
    tight = Budget(area=6)
    picked = extract_dp(program2, problem2, tight)
    assert [(s.start, s.stop) for s in picked] == [(0, 1)]
    assert skip_cost(program2, problem2, 1) == 36 + 2 * 2 + 4
    assert total(picked, program2, problem2) == cost(picked[0], problem2, 36) + skip_cost(program2, problem2, 1)


@pytest.mark.parametrize("name", ["toffoli/tof-3.qasm", "algorithms/qft-4.qasm"])
def test_extract_dp_is_a_chain_no_dearer_than_the_greedy_pick(name):
    from miniflash.resynthesis import extract_dp, total
    problem = read_qasm(ROOT / "benchmarks" / name)
    program = make("layout").solve(problem)
    picked = extract_dp(program, problem)
    for a, b in zip(picked, picked[1:]):
        assert a.stop <= b.start
    for slab in picked:
        for w in slab.windows:
            w.check(program)
    fits = {s.start for s in slabs(program) if s.steps == 1}
    assert {t for s in picked for t in range(s.start, s.stop)} == fits
    assert total(picked, program, problem) <= total(extract_greedy(program, problem, min_gain=0), program, problem)


def t_window():
    problem = Problem.from_pairs(4, [Pair(1, MAGIC, "t")])
    program = Program(6, 6, [step_with([(0, [(0, 3), (0, 4)], 1, None)], consumed=0)])
    verify(problem, program)
    (w,) = close(program, 0, 1)
    return problem, program, w


def test_spec_ports_stabilizers_and_forbidden_cubes():
    pytest.importorskip("stim")
    from miniflash.resynthesis.spec import layers, pipes, port_names, spec, stabilizers
    problem, program = two_far_apart()
    w = close(program, 0, 1)[0]                       # cx(q0, q1) in the box rows 0-1, cols 1-3
    assert layers(w) == 2 and port_names(w) == ["in q0", "in q1", "out q0", "out q1"]
    assert stabilizers(w, problem) == ["XIXX", "ZIZI", "IXIX", "IZZZ"]
    s = spec(w, program, problem)
    assert (s["max_i"], s["max_j"], s["max_k"]) == (3, 2, 3)
    assert [(p["location"], p["direction"], p["z_basis_direction"]) for p in s["ports"]] == [
        ([0, 1, 0], "+K", "I"), ([2, 1, 0], "+K", "I"), ([0, 1, 3], "-K", "I"), ([2, 1, 3], "-K", "I")]
    assert s["optional"]["forbidden_cubes"] == [[0, 0, 0], [1, 0, 0], [1, 1, 0], [2, 0, 0]]
    # control leaves upwards on the lower layer, the ancilla climbs, the X half runs above
    assert pipes(w, program) == {"I": {(0, 0, 2), (1, 1, 2)}, "J": {(0, 0, 1), (1, 0, 2)},
                                 "K": {(0, 0, 1), (0, 1, 0), (0, 1, 1), (0, 1, 2), (2, 1, 0), (2, 1, 1), (2, 1, 2)}}
    # bystanders and the magic tile are carved out of a wider window
    (whole,) = close(program, 1, 2)                   # cx(q1, q2) with q0 and q3 standing by
    f = {tuple(c) for c in spec(whole, program, problem)["optional"]["forbidden_cubes"]}
    for q, tile in whole.bystanders.items():
        assert all((tile[1] - whole.box.c0, tile[0] - whole.box.r0, k) in f for k in range(3))
    # a T merge: one magic port at the magic tile, towards the route, a Z spider leg
    problem, program, w = t_window()
    s = spec(w, program, problem)
    assert stabilizers(w, problem) == ["XXX", "ZIZ", "IZZ"]
    assert (s["ports"][2]["location"], s["ports"][2]["direction"], s["ports"][2]["z_basis_direction"]) == ([2, 0, 1], "-I", "J")
    assert [2, 0, 1] not in s["optional"]["forbidden_cubes"] and [2, 0, 2] in s["optional"]["forbidden_cubes"]
    assert spec(w, program, problem, depth=1)["max_k"] == 2


def test_lassynth_agrees_with_the_tile_model_and_halves_a_t_merge():
    pytest.importorskip("stim")
    pytest.importorskip("z3")
    pytest.importorskip("lassynth")
    from miniflash.resynthesis.synth import SAT, UNSAT, best, check, kissat_binary, minimize
    try:
        kissat_binary()
    except FileNotFoundError:
        pytest.skip("no kissat")
    problem, program = two_far_apart()
    w = close(program, 0, 1)[0]
    assert check(w, program, problem).status == SAT
    assert check(w, program, problem, qubit_z="J").status == UNSAT      # the convention is not a free choice
    assert [(r.depth, r.status) for r in minimize(w, program, problem)] == [(1, UNSAT), (2, SAT)]
    problem, program, w = t_window()
    assert check(w, program, problem).status == SAT
    results = minimize(w, program, problem)
    assert [(r.depth, r.status) for r in results] == [(1, SAT)] and best(results).depth == 1


def needs_lassynth():
    pytest.importorskip("stim")
    pytest.importorskip("z3")
    pytest.importorskip("lassynth")
    from miniflash.resynthesis.synth import kissat_binary
    try:
        kissat_binary()
    except FileNotFoundError:
        pytest.skip("no kissat")


def test_cell_keeps_the_solution_and_round_trips():
    needs_lassynth()
    from miniflash.resynthesis import Cell
    from miniflash.resynthesis.synth import cell
    problem, program, w = t_window()
    c = cell(w, program, problem)
    assert (c.depth, c.saved, c.proven) == (1, 1, True)
    assert c.magic_cubes() == [(2, 0, 1)]
    ks = [p.cube for p in c.pipes() if p.axis == "K"]
    assert ks == [(0, 1, 0), (0, 1, 1)]               # in through the bottom, out through the top
    back = Cell.from_dict(c.to_dict())
    assert back.window == c.window and back.depth == 1 and back.pipes() == c.pipes()
    # no gain: the cell is the tile program's own routing, pipe for pipe
    from miniflash.resynthesis.spec import pipes
    problem, program = two_far_apart()
    w = close(program, 0, 1)[0]
    c = cell(w, program, problem)
    assert (c.depth, c.saved) == (2, 0)
    assert {a: {p.cube for p in c.pipes() if p.axis == a} for a in "IJK"} == pipes(w, program)


def test_cells_are_drawn_in_place_of_their_slabs(tmp_path):
    needs_lassynth()
    import json
    from miniflash.gltf import STRIDE
    from miniflash.resynthesis import Slab, lower_cells, timeline, write_cells
    from miniflash.resynthesis.synth import cells
    # T on q1 (one step, one layer after), then cx(q0, q1) left as the tile program has it
    problem = Problem.from_pairs(4, [Pair(1, MAGIC, "t"), Pair(0, 1, "cx")])
    program = Program(6, 6, [step_with([(0, [(0, 3), (0, 4)], 1, None)], consumed=0),
                             step_with([(1, [(0, 1), (0, 2), (1, 2)], 0, 1)])])
    verify(problem, program)
    plan = cells([Slab(0, 1, tuple(close(program, 0, 1)))], program, problem)
    assert timeline(program, plan) == ({0: 0, 1: 1}, 3)
    assert timeline(program, plan, compact=False) == ({0: 0, 1: 2}, 4)
    pipes, cubes, magic, overlays, total = lower_cells(program, plan)
    assert total == 3 and magic == [(STRIDE * 5, 0, 0)]
    ks = {(p.lo, p.hi) for p in pipes if p.axis == "K"}
    for q, (row, col) in SEATS.items():               # every qubit crosses from the slab into the next step
        assert ((STRIDE * col, STRIDE * row, 0), (STRIDE * col, STRIDE * row, STRIDE)) in ks
    assert len({(p.lo, p.hi) for p in pipes}) == len(pipes)
    assert all(0 <= p.lo[2] and p.hi[2] <= STRIDE * (total - 1) for p in pipes)
    # in place the qubits idle one layer above the shallow cell
    inplace = lower_cells(program, plan, compact=False)
    assert inplace[4] == 4 and len(inplace[0]) == len(pipes) + len(SEATS)
    assert write_cells(program, plan, tmp_path / "c.gltf") == 3
    assert json.loads((tmp_path / "c.gltf").read_text())["nodes"]
