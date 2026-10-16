"""Cell: a window and the lassynth solution that replaces it.

A cell keeps the window it came from (box, steps, pairs, participants), the depth
in layers lassynth needed, and the solution itself: lassynth's arrays after its
default clean-up, which drops unconnected pieces and colours the K pipes.

Coordinates inside a cell are lassynth's: cube (i, j, k) is column box.c0 + i, row
box.r0 + j, and layer k - 1 of the cell, layer 0 of the volume holding only the
open ends of the input ports.

Colours. lassynth gives every pipe an orientation, which faces are Z-type; the
tile program draws a pipe in one colour. A cell's pipes are drawn so that a qubit
pipe is red and a merge leaving a qubit through its Z side is blue, as in the
tile program, whenever the solution changes no colour along time. A K pipe whose
two ends disagree carries a Hadamard wall and is drawn yellow. lassynth does not
charge for walls and picks among equivalent colourings freely, so a solution may
hold more walls than it needs.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import List, NamedTuple, Tuple

from .window import Box, Component, Window


class CellPipe(NamedTuple):
    axis: str                      # "I" | "J" | "K"
    cube: Tuple[int, int, int]     # the pipe runs from this cube to its successor along `axis`
    parity: int                    # 0 red, 1 blue
    wall: bool                     # a K pipe that changes colour: a Hadamard


@dataclass(frozen=True)
class Cell:
    window: Window
    depth: int          # layers
    lasre: dict
    proven: bool = False  # the depth below was shown unsatisfiable

    @classmethod
    def of(cls, window: Window, lasre: dict, proven: bool = False) -> "Cell":
        """From a raw lassynth solution: run lassynth's default clean-up on a copy."""
        from lassynth import LatticeSurgerySolution
        cleaned = LatticeSurgerySolution(lasre=copy.deepcopy(lasre)).after_default_optimizations().lasre
        return cls(window, cleaned["n_k"] - 1, cleaned, proven)

    @property
    def saved(self) -> int:
        """Layers below the window's own."""
        return 2 * self.window.steps - self.depth

    def pipes(self) -> List[CellPipe]:
        a = self.lasre
        out = []
        for i in range(a["n_i"]):
            for j in range(a["n_j"]):
                for k in range(a["n_k"]):
                    if a["ExistI"][i][j][k]:
                        out.append(CellPipe("I", (i, j, k), 1 - int(a["ColorI"][i][j][k]), False))
                    if a["ExistJ"][i][j][k]:
                        out.append(CellPipe("J", (i, j, k), int(a["ColorJ"][i][j][k]), False))
                    if a["ExistK"][i][j][k]:
                        lo, hi = int(a["ColorKM"][i][j][k]), int(a["ColorKP"][i][j][k])
                        out.append(CellPipe("K", (i, j, k), lo, lo != hi))
        return out

    def y_cubes(self) -> List[Tuple[int, int, int]]:
        a = self.lasre
        return [(i, j, k) for i in range(a["n_i"]) for j in range(a["n_j"]) for k in range(a["n_k"]) if a["NodeY"][i][j][k]]

    def magic_cubes(self) -> List[Tuple[int, int, int]]:
        """Open cubes of the magic ports, in port order."""
        n = 2 * len(self.window.participants)
        return [tuple(c) for c in self.lasre["port_cubes"][n:]]

    @property
    def walls(self) -> int:
        return sum(1 for p in self.pipes() if p.wall)

    def to_dict(self) -> dict:
        w = self.window
        return {"window": {"box": w.box.to_dict(), "start": w.start, "stop": w.stop,
                           "components": [[c.step, c.pair, sorted(c.tiles), list(c.qubits), c.magic] for c in w.components],
                           "bystanders": {str(q): list(t) for q, t in w.bystanders.items()},
                           "idle_magic": [list(m) for m in w.idle_magic]},
                "depth": self.depth, "proven": self.proven, "lasre": json.loads(json.dumps(self.lasre, default=list))}

    @classmethod
    def from_dict(cls, d: dict) -> "Cell":
        w = d["window"]
        comps = tuple(Component(s, p, frozenset(tuple(t) for t in tiles), tuple(qs), tuple(m) if m else None)
                      for s, p, tiles, qs, m in w["components"])
        window = Window(Box(**w["box"]), w["start"], w["stop"], comps,
                        {int(q): tuple(t) for q, t in w["bystanders"].items()}, tuple(tuple(m) for m in w["idle_magic"]))
        return cls(window, d["depth"], d["lasre"], d["proven"])
