from dataclasses import dataclass


@dataclass(frozen=True)
class FactorySpec:
    name: str
    dim_i: int
    dim_j: int
    interval_k: int


FACTORIES = {
    "15-to-1": FactorySpec("15-to-1", dim_i=6, dim_j=2, interval_k=11),
    "t-cultivation": FactorySpec("t-cultivation", dim_i=2, dim_j=1, interval_k=2),
}


def get_factory(name: str) -> FactorySpec:
    """Look up a magic-state factory preset by name.

    :param name: str key in FACTORIES ("15-to-1" | "t-cultivation").
    :returns: FactorySpec.
    :raises ValueError: on unknown name.
    """
    if name not in FACTORIES:
        raise ValueError(f"unknown factory {name!r}; choices: {sorted(FACTORIES)}")
    return FACTORIES[name]


GADGET_M1 = ("ZZ", ("data", "magic"))

_CORRECTIONS = {(0, 0): "I", (0, 1): "Z", (1, 0): "I", (1, 1): "Z"}
_CORRECTIONS_DAGGER = {(0, 0): "I", (0, 1): "Z", (1, 0): "Z", (1, 1): "I"}


def correction_for(outcomes, dagger: bool) -> str:
    """Clifford correction of the M1 gadget for a given outcome pair.

    :param outcomes: (s, r) ints (0|1) — ZZ outcome and magic readout.
    :param dagger: bool, True for tdg.
    :returns: "I" or "Z" (Pauli-frame correction on the data qubit).
    """
    return (_CORRECTIONS_DAGGER if dagger else _CORRECTIONS)[tuple(outcomes)]
