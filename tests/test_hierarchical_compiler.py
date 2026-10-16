"""Reuse and composition checked against the independent program verifier."""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from miniflash import Compiler, Def, Factory, Hierarchy, Layout, Mapping, Pair, Placement, Problem, Program, make, verify
from miniflash.route import Route, Segment, check_route, shortest_magic_route, shortest_route
from miniflash.synthesis import routing
from miniflash.synthesis.hierarchical import Call, Cells, _shortest, compose, extract
from miniflash.synthesis.placement import Workload, _Proxy

ROOT = Path(__file__).resolve().parents[1]


def definition(name, n, body):
    return Def(name, "test", tuple(f"q{i}" for i in range(n)), tuple(range(n)), tuple(range(n)), tuple(body))


def repeated():
    cell = definition("cell", 3, [("cx", (0, 1), None), ("cx", (1, 2), None), ("t", (0,), None)])
    other = definition("other", 3, cell.body)  # identical body with a different name
    root = definition("root", 4, [("cell", (0, 1, 2), None), ("other", (0, 1, 2), None),
                                  ("cell", (1, 2, 3), None), ("cx", (3, 0), None)])
    return Hierarchy([root, cell, other], "root")


def test_cut_covers_pairs_and_only_reuses_identical_global_assignments():
    h = repeated()
    cells = extract(h, 3)
    assert len(cells.problems) == 3
    assert [call.cell for call in cells.calls] == [0, 0, 1, 2]
    assert [i for call in cells.calls for i in range(call.start, call.stop)] == list(range(len(h.problem.pairs)))
    assert all(len(cell.pairs) <= 3 for cell in cells.problems)
    assert cells.workload(h.n_qubits).interaction() == h.problem.interaction()
    assert sum(len(layer) * count for layer, count in zip(cells.workload(h.n_qubits).layers,
                                                         cells.workload(h.n_qubits).frequencies)) == len(h.problem.pairs)


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_bad_cell_limit(limit):
    with pytest.raises(ValueError, match="positive integer"):
        extract(repeated(), limit)


def test_weighted_proxy_matches_explicitly_repeated_layers_and_swap_cost():
    p = Problem.from_pairs(4, [Pair(0, 1, "cx"), Pair(2, 3, "cx"), Pair(0, -1, "t")])
    layers, counts = [[0, 1], [2]], [7, 2]
    weighted = Workload(p, layers, counts)
    expanded = Workload(p, [layer for layer, count in zip(layers, counts) for _ in range(count)], [1] * sum(counts))
    mapping = Mapping.paired(2, 2, 4)
    a, b = _Proxy(p, mapping, workload=weighted), _Proxy(p, mapping, workload=expanded)
    assert weighted.interaction() == expanded.interaction()
    assert a.total == b.total
    assert a.swap_delta(0, 3, mapping.qubits[3])[0] == b.swap_delta(0, 3, mapping.qubits[3])[0]


def test_routes_each_distinct_cell_once_and_remaps_every_pair(monkeypatch):
    h = repeated()
    original, routed = routing.route, []

    def record(problem, mapping, *args, **kwargs):
        routed.append(problem.pairs)
        assert set(mapping.qubits) == set(h.problem.qubits)  # unused qubits stay as obstacles
        return original(problem, mapping, *args, **kwargs)

    monkeypatch.setattr(routing, "route", record)
    compiler = make("layout", anneal=False)
    program = compiler.solve_hierarchy(h, cell_limit=3)
    report = verify(h.problem, program)
    assert len(routed) == compiler.hierarchy_stats["cells"] == 3
    assert compiler.hierarchy_stats["calls"] == 4
    assert compiler.hierarchy_stats["routed_pairs"] == 7
    assert len(report.when) == 10
    assert compiler.windows and compiler.partitions
    assert "compose_s" in compiler.profile[0]


def test_later_call_can_use_qubits_released_before_the_first_call_finishes():
    a = Problem.from_pairs(3, [Pair(0, 1, "cx"), Pair(2, -1, "t"), Pair(2, -1, "t")])
    b = Problem.from_pairs(3, [Pair(0, 1, "cx")])
    problem = Problem.from_pairs(3, a.pairs + b.pairs)
    mapping = Mapping.sparse(2, 2, 3)
    templates = [[[shortest_route(0, 0, 1, mapping)], [shortest_magic_route(1, 2, mapping)],
                  [shortest_magic_route(2, 2, mapping)]], [[shortest_route(0, 0, 1, mapping)]]]
    cells = Cells((a, b), (Call(0, 0, 3), Call(1, 3, 4)))
    result = compose(problem, mapping, cells, templates)
    report = verify(problem, Program.from_routes(problem, mapping, result))
    assert report.when[0] < report.when[3] < report.when[2]


def test_crossing_cached_routes_are_rerouted_or_delayed():
    mapping = Mapping(5, 5, {0: (0, 2), 1: (2, 4), 2: (4, 2), 3: (2, 0)})
    a = Problem.from_pairs(4, [Pair(0, 1, "cx")])
    b = Problem.from_pairs(4, [Pair(2, 3, "cx")])
    problem = Problem.from_pairs(4, a.pairs + b.pairs)
    templates = [[[Route(0, (Segment((1, 2), (2, 2)), Segment((2, 2), (2, 3))))]],
                 [[Route(0, (Segment((3, 2), (2, 2)), Segment((2, 2), (2, 1))))]]]
    result = compose(problem, mapping, Cells((a, b), (Call(0, 0, 1), Call(1, 1, 2))), templates)
    assert len(verify(problem, Program.from_routes(problem, mapping, result)).when) == 2


def test_unrestricted_path_fallback_can_follow_more_than_three_segments():
    # A one-tile-wide serpentine corridor, surrounded by bystander qubits.
    path = [(1, 1), (1, 2), (1, 3), (2, 3), (3, 3), (3, 4), (3, 5), (4, 5), (5, 5), (5, 6)]
    seats = {0: (0, 1), 1: (5, 7)}
    for r in range(6):
        for c in range(8):
            if (r, c) not in path and (r, c) not in seats.values():
                seats[len(seats)] = (r, c)
    mapping = Mapping(6, 8, seats)
    problem = Problem.from_pairs(len(seats), [Pair(0, 1, "cx")])
    assert not routing.candidates(problem, mapping, 0, set(), set())
    route = _shortest(problem, mapping, 0, set(), set())
    check_route(route, problem.pairs[0], mapping)
    assert len(route.segments) > 3


@pytest.mark.parametrize("spacing", [False, "whole", True])
@pytest.mark.parametrize("factories", [0, 1, 2])
def test_supply_is_global_across_reused_cells(spacing, factories):
    # One delivery tile forces the cooldown case to wait across call boundaries.
    cell = definition("cell", 1, [("t", (0,), None)])
    root = definition("root", 1, [("cell", (0,), None)] * 4)
    h = Hierarchy([cell, root], "root")
    mapping = Mapping(3, 3, {0: (1, 1)}, ((0, 0),))
    placed = Placement(mapping, Layout.identity(1), 0, 0)
    factory = Factory(1, 1, 7)
    compiler = Compiler(routing=True, spacing=spacing, placement=placed, factory=factory, factories=factories)
    program = compiler.solve_hierarchy(h, cell_limit=1)
    report = verify(h.problem, program, factory, factories)
    if not factories:
        assert list(report.when.values()) == [0, 7, 14, 21]
    else:
        assert report.when[0] >= (7 + factories - 1) // factories
    assert compiler.hierarchy_stats["cells"] == 1


def test_parallel_magic_steps_wait_for_their_full_global_supply():
    cell = definition("cell", 2, [("t", (0,), None), ("t", (1,), None)])
    root = definition("root", 4, [("cell", (0, 1), None), ("cell", (2, 3), None), ("cell", (0, 1), None)])
    h = Hierarchy([cell, root], "root")
    factory = Factory(1, 1, 5)
    compiler = Compiler(routing=True, factory=factory, factories=1)
    report = verify(h.problem, compiler.solve_hierarchy(h, cell_limit=2), factory, 1)
    assert report.when[0] == report.when[1] == 10
    assert report.when[2] == report.when[3] == 20
    assert report.when[4] == report.when[5] == 30


@pytest.mark.parametrize("seed", range(5))
def test_mixed_repeated_calls_with_spacing_and_factories(seed):
    rng = random.Random(seed)
    cells = []
    for k in range(3):
        body = []
        for _ in range(6):
            a, b = rng.sample(range(4), 2)
            body.append(("cx", (a, b), None) if rng.random() < 0.6 else ("t", (a,), None))
        cells.append(definition(f"c{k}", 4, body))
    body = [(f"c{rng.randrange(3)}", tuple(rng.sample(range(6), 4)), None) for _ in range(8)]
    body += body[:4]
    h = Hierarchy(cells + [definition("root", 6, body)], "root")
    factory = Factory(1, 1, 5)
    compiler = make("layout", seed=seed, extra=0, factory=factory, factories=2)
    program = compiler.solve_hierarchy(h, cell_limit=6)
    assert len(verify(h.problem, program, factory, 2).when) == 72


@pytest.mark.parametrize("mode", ["weighted", "flat"])
def test_shape_search_keeps_selected_state_and_can_be_reused_for_flat_solves(mode):
    h = repeated()
    compiler = make("layout", shapes=3, anneal=False)
    program = compiler.solve_hierarchy(h, cell_limit=3, placement_mode=mode)
    verify(h.problem, program)
    assert program.volume == min(p["volume"] for p in compiler.profile)
    assert tuple(compiler.shape) in [tuple(p["shape"]) for p in compiler.profile if p["volume"] == program.volume]
    verify(h.problem, compiler.solve(h.problem))
    assert compiler.hierarchy_stats == {} and "hierarchical" not in compiler.name


def test_empty_hierarchy_and_invalid_configuration():
    h = Hierarchy([definition("root", 2, [("h", (0,), None)])], "root")
    compiler = make("layout")
    assert verify(h.problem, compiler.solve_hierarchy(h)).when == {}
    with pytest.raises(ValueError, match="routing=True"):
        Compiler().solve_hierarchy(h)
    with pytest.raises(ValueError, match="placement"):
        compiler.solve_hierarchy(h, placement_mode="unknown")


def test_driver_accepts_json_in_both_modes_and_rejects_qasm_for_reuse():
    from main import compile
    path = ROOT / "benchmarks/qualtran/add-4.json"
    for hierarchical in (False, True):
        problem, program, report = compile(path, hierarchical=hierarchical, cell_limit=12, anneal=False)
        assert len(report.when) == len(problem.pairs) == 48
    with pytest.raises(ValueError, match="hierarchy .json"):
        compile(path.with_suffix(".qasm"), hierarchical=True)
