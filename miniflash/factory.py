"""Magic-state factories: charge a finished program for the supply behind its magic tiles.

    from miniflash import Factory, charge
    bill = charge(program, Factory.parse("3x3x11"))      # 3 x 3 tiles, one state every 11 steps
    bill.total                                           # program volume + factory volume

A program consumes a magic state whenever a MAGIC vertex carries a pair id (an idle
magic tile has id -1). The factories that supply those states sit off the chip, so the
compiler never sees them; this module prices them afterwards. One factory of
`height x width` tiles yields one state every `period` steps and is kept for the whole
program, so

    factory_volume = factories x height x width x steps
    total          = program.volume + factory_volume

How many factories a program needs is the `sizing` of the Factory:

    peak    the most states any single step consumes (one factory per concurrently
            consumed tile; the right count when delivery is already limited to one
            state per tile every `period` steps, as with Compiler(magic_cooldown=period))
    rate    the most states consumed in any window of `period` consecutive steps, which
            is what `factories` producing one state per `period` steps can sustain; it
            equals `peak` under the delivery limit and exceeds it without one
    tiles   every magic tile consumed at least once (the compaction leaves only the
            tiles the program uses, so this is the supply the layout provides for)

When the compiler was itself given a supply (Compiler(factories=F, period=P)), price
exactly those: charge(program, Factory(3, 3, P), factories=F).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from .mapping import Coord
from .program import IDLE, MAGIC, Program

SIZINGS = ("peak", "rate", "tiles")


@dataclass(frozen=True)
class Factory:
    height: int = 3
    width: int = 3
    period: int = 11          # steps per magic state
    sizing: str = "peak"

    def __post_init__(self):
        if min(self.height, self.width, self.period) < 1:
            raise ValueError("factory height, width, and period must be positive")
        if self.sizing not in SIZINGS:
            raise ValueError(f"sizing must be one of {SIZINGS}, not {self.sizing!r}")

    @property
    def area(self) -> int:
        return self.height * self.width

    @classmethod
    def parse(cls, text: str) -> "Factory":
        """'HxWxP' or 'HxWxP:sizing', e.g. '3x3x11' or '3x3x11:rate'."""
        dims, _, sizing = text.partition(":")
        h, w, p = (int(x) for x in dims.lower().split("x"))
        return cls(h, w, p, sizing or cls.sizing)

    def __str__(self) -> str:
        return f"{self.height}x{self.width}x{self.period}:{self.sizing}"


@dataclass(frozen=True)
class Bill:
    factory: Factory
    steps: int
    consumed: Tuple[int, ...]      # states consumed per step
    tiles: Set[Coord]              # magic tiles consumed at least once
    factories: int

    @property
    def states(self) -> int:
        return sum(self.consumed)

    @property
    def peak(self) -> int:
        return max(self.consumed, default=0)

    @property
    def factory_volume(self) -> int:
        return self.factories * self.factory.area * self.steps

    def total(self, program_volume: int) -> int:
        return program_volume + self.factory_volume


def supply_limits(factory: Optional[Factory], factories: int = 0) -> Tuple[int, int, int]:
    """(cooldown, factories, period) that the router and the verifier enforce.

    No factory: unlimited supply. A factory alone: one of it behind every magic tile,
    so a tile yields a state every `factory.period` steps. With `factories` = F > 0:
    F shared factories feed the whole chip (through the delivery rings of
    miniflash.supply), F states per `factory.period` steps; the default Factory()
    when none is given."""
    if factories < 0:
        raise ValueError("factories must be non-negative")
    if factories:
        return 1, factories, (factory or Factory()).period
    if factory is None:
        return 1, 0, 1
    return factory.period, 0, factory.period


def demand(program: Program) -> Tuple[List[int], Set[Coord]]:
    """States consumed per step, and the magic tiles consumed at least once."""
    consumed, tiles = [], set()
    for step in program.steps:
        used = [v.tile for v in step.of_kind(MAGIC) if v.id != IDLE]
        consumed.append(len(used))
        tiles.update(used)
    return consumed, tiles


def count(consumed: List[int], tiles: Set[Coord], factory: Factory) -> int:
    if factory.sizing == "tiles":
        return len(tiles)
    if factory.sizing == "peak":
        return max(consumed, default=0)
    window = factory.period
    return max((sum(consumed[i:i + window]) for i in range(max(1, len(consumed) - window + 1))), default=0)


def charge(program: Program, factory: Factory = Factory(), factories: Optional[int] = None) -> Bill:
    """Price the program; `factories` overrides the sizing with a fixed count, e.g. the
    number the compiler was given (Compiler(factories=...))."""
    consumed, tiles = demand(program)
    return Bill(factory, len(program.steps), tuple(consumed), tiles,
                factories if factories is not None else count(consumed, tiles, factory))
