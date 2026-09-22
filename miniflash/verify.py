"""Independent checker: does a Program execute a Problem under the execution model?

It reads only the step graphs, never the solver's intermediate objects, so it
also checks programs produced by adapters for other compilers.

Per step: every qubit sits on exactly one tile; edges join 4-neighbours; every
component with an edge realises exactly one pair: its route and consumed-magic
vertices all carry that pair's id, the Z party joins the component through its
Z side (above or below), the X party through its X side (left or right; a magic
tile through any side), and no other qubit or foreign magic tile is inside.
Across steps: each pair runs exactly once, after all its predecessors; a qubit
moves at most one tile per axis per step (rows and columns walk together), and a
step where anything moves has no edges.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .circuit import Problem
from .factory import Factory, supply_limits
from .program import IDLE, MAGIC, QUBIT, ROUTE, Program, Step


class VerifyError(AssertionError):
    pass


@dataclass(frozen=True)
class Report:
    steps: int
    walk_steps: int
    height: int
    width: int
    when: Dict[int, int]  # pair -> step it executed

    @property
    def volume(self) -> int:
        return self.steps * self.height * self.width


def _adjacent(a, b) -> bool:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def _check_step(t: int, step: Step, problem: Problem, program: Program) -> Dict[int, int]:
    """Return {pair: 1} for pairs executed at this step; raise on a violation."""
    for v in step.vertices.values():
        if not (0 <= v.tile[0] < program.height and 0 <= v.tile[1] < program.width):
            raise VerifyError(f"step {t}: vertex {v.tile} off the grid")
    seats = step.qubit_tiles()
    qubit_vertices = step.of_kind(QUBIT)
    if len(qubit_vertices) != problem.n_qubits or set(seats) != set(problem.qubits):
        raise VerifyError(f"step {t}: expected every qubit exactly once, found {sorted(v.id for v in qubit_vertices)}")
    for e in step.edges:
        a, b = tuple(e)
        if not _adjacent(a, b):
            raise VerifyError(f"step {t}: edge {a}-{b} joins non-adjacent tiles")
    done: Dict[int, int] = {}
    for comp in step.components():
        vs = [step.vertices[x] for x in comp]
        ids = {v.id for v in vs if v.kind == ROUTE} | {v.id for v in vs if v.kind == MAGIC and v.id != IDLE}
        if len(ids) != 1:
            raise VerifyError(f"step {t}: component {sorted(comp)} carries pair ids {sorted(ids)}")
        (p,) = ids
        if not 0 <= p < len(problem.pairs):
            raise VerifyError(f"step {t}: unknown pair {p}")
        if p in done:
            raise VerifyError(f"step {t}: pair {p} realised twice")
        pair = problem.pairs[p]
        if any(v.kind == MAGIC and v.id == IDLE for v in vs):
            raise VerifyError(f"step {t}: pair {p} merges an idle magic tile")
        qs = {v.id: v.tile for v in vs if v.kind == QUBIT}
        if set(qs) != set(pair.qubits):
            raise VerifyError(f"step {t}: pair {p} {pair} joins qubits {sorted(qs)}")
        adj = step.neighbours()
        z_tile = qs[pair.z]
        for n in adj[z_tile]:
            if n[1] != z_tile[1]:
                raise VerifyError(f"step {t}: pair {p} leaves q{pair.z} through the wrong side {z_tile}-{n}")
        if pair.is_magic:
            magics = [v for v in vs if v.kind == MAGIC]
            if len(magics) != 1:
                raise VerifyError(f"step {t}: pair {p} consumes {len(magics)} magic tiles")
            x_tile = magics[0].tile
        else:
            x_tile = qs[pair.x]
            for n in adj[x_tile]:
                if n[0] != x_tile[0]:
                    raise VerifyError(f"step {t}: pair {p} enters q{pair.x} through the wrong side {x_tile}-{n}")
        if not adj[z_tile] or not adj[x_tile]:
            raise VerifyError(f"step {t}: pair {p} does not reach both parties")
        done[p] = 1
    # route vertices and consumed magic must belong to some component
    for v in step.vertices.values():
        if v.kind == ROUTE and v.id not in done:
            raise VerifyError(f"step {t}: route tile {v.tile} of pair {v.id} is disconnected")
        if v.kind == MAGIC and v.id != IDLE and v.id not in done:
            raise VerifyError(f"step {t}: magic {v.tile} consumed by pair {v.id} without a route")
    return done


def _check_move(t: int, before: Step, after: Step) -> bool:
    """True when some qubit moved between the two steps."""
    a, b = before.qubit_tiles(), after.qubit_tiles()
    moved = False
    for q, tile in b.items():
        d = max(abs(tile[0] - a[q][0]), abs(tile[1] - a[q][1]))
        if d > 1:
            raise VerifyError(f"step {t}: q{q} jumps from {a[q]} to {tile}")
        moved |= d == 1
    if moved and not after.is_walk:
        raise VerifyError(f"step {t}: qubits move and gates execute in the same step")
    return moved


def _check_cooldown(t: int, step: Step, last: Dict, cooldown: int, walked: bool) -> None:
    """A magic tile consumed at step s may not be consumed again before step s + cooldown.
    Walk steps carry no magic tiles, and a stage after a walk re-provisions its delivery
    tiles at new coordinates, so the record restarts after every walk. An idle step
    without movement keeps the record."""
    if walked:
        last.clear()
        return
    for v in step.of_kind(MAGIC):
        if v.id == IDLE:
            continue
        if v.tile in last and t - last[v.tile] < cooldown:
            raise VerifyError(f"step {t}: magic {v.tile} consumed by pair {v.id} only "
                              f"{t - last[v.tile]} steps after step {last[v.tile]}, cooldown {cooldown}")
        last[v.tile] = t


def verify(problem: Problem, program: Program, factory: Optional[Factory] = None, factories: int = 0) -> Report:
    """Check the program against the problem and its magic supply (see
    factory.supply_limits): with a factory alone, that no delivery tile is consumed again
    within factory.period steps; with factories = F > 0, that the k-th state consumed
    comes no earlier than routed step k x period / F."""
    magic_cooldown, factories, period = supply_limits(factory, factories)
    when: Dict[int, int] = {}
    walks = 0
    last_magic: Dict = {}
    states = 0
    for t, step in enumerate(program.steps):
        walked = t > 0 and _check_move(t, program.steps[t - 1], step)
        walks += walked
        if magic_cooldown > 1:
            _check_cooldown(t, step, last_magic, magic_cooldown, walked)
        if factories:
            states += sum(1 for v in step.of_kind(MAGIC) if v.id != IDLE)
            if states > ((t - walks) * factories) // period:
                raise VerifyError(f"step {t}: state {states} consumed before {factories} factories at one per {period} steps "
                                  f"could produce it (routed step {t - walks})")
        for p in _check_step(t, step, problem, program):
            if p in when:
                raise VerifyError(f"pair {p} executed at steps {when[p]} and {t}")
            for j in problem.preds[p]:
                if j not in when or when[j] >= t:
                    raise VerifyError(f"step {t}: pair {p} runs before its predecessor {j}")
            when[p] = t
    missing = [p for p in range(len(problem.pairs)) if p not in when]
    if missing:
        raise VerifyError(f"{len(missing)} pairs never executed, first {missing[:5]}")
    return Report(len(program.steps), walks, program.height, program.width, when)
