"""The miniflash driver + CLI: ``python main.py circuit.qasm -o out/``.

``compile()`` runs the package pipeline to the Program IR and is the
library entry point (``from main import compile``); ``main()`` emits the
IR (``build_layout`` -> ``write_gltf``) and assembles stats.
"""

import argparse
import json
from pathlib import Path

import miniflash as flash


def compile(qasm_path, cache_dir=".miniflash-cache", max_gates=16, side_ports=False, factory="15-to-1", die_dims=None, factories=1, budget_s=600):
    """Compile a circuit to the Program IR: parse -> partition -> synthesize -> elaborate.

    Partitioning is coarse-first: regions start at whole-circuit granularity
    (capped at 16 qubits) and split in place when a region exhausts its SAT
    budget (16 -> 12 -> 8 -> 6 -> 4 -> 2).

    :param qasm_path: str | Path to a .qasm file.
    :param cache_dir: str, LaSsynth disk cache directory.
    :param max_gates: int, floor of the per-region gate cap.
    :param side_ports: bool, swap through cell side faces.
    :param factory: str preset name or FactorySpec.
    :param die_dims: (width, rows | None) or None for 1-D.
    :param factories: int, magic state factory units.
    :param budget_s: int, SAT budget in seconds per region.
    :returns: Program.
    """
    circuit = flash.parse(qasm_path)
    top = min(circuit.num_qubits, 16)
    caps = [top] + [cap for cap in (12, 8, 6, 4, 2) if cap < top]
    gates_cap = lambda cap: max(max_gates, cap * cap)
    partitioned = flash.partition(circuit, max_qubits=caps[0], max_gates=gates_cap(caps[0]))
    regions, events = partitioned.regions, partitioned.events
    if not regions and not events:
        raise RuntimeError("compile: circuit has no synthesizable regions")

    factory_spec = (factory if isinstance(factory, flash.FactorySpec) else flash.get_factory(factory)) if events else None
    rung_of = {id(region): 0 for region in regions}
    while True:
        try:
            floorplan, cells, channels = flash.synthesize(partitioned, cache_dir=cache_dir, side_ports=side_ports, die_dims=die_dims, budget_s=budget_s)
            break
        except RuntimeError as error:
            failed = getattr(error, "region", None)
            if failed is None:
                raise
            rung = rung_of[id(failed)] + 1
            if rung >= len(caps):
                raise
            index = next(position for position, region in enumerate(regions) if region is failed)
            subs = flash.split_region(failed, caps[rung], gates_cap(caps[rung]))
            regions[index : index + 1] = subs
            for sub in subs:
                rung_of[id(sub)] = rung

    return flash.elaborate(floorplan, cells, events=events, channels=channels, factory=factory_spec, factories=factories)


def main():
    """CLI entry point: compile to the Program IR, then write the glTF scene.

    :returns: None; prints the glTF path.
    """
    parser = argparse.ArgumentParser(description="Compile a Clifford+T OpenQASM 2.0 circuit into a lattice-surgery layout (glTF 2.0)")
    parser.add_argument("qasm",                                                                          help="input .qasm file")
    parser.add_argument("-o", "--out",         default="layout.gltf",                                    help="output .gltf path")
    parser.add_argument("--cache-dir",         default=".miniflash-cache",                               help="cell cache directory")
    parser.add_argument("--max-gates",         type=int, default=16,                                     help="floor of the per-region gate cap")
    parser.add_argument("--side-ports",        action="store_true",                                      help="swap through cell side faces")
    parser.add_argument("--factory",           choices=sorted(flash.FACTORIES), default="15-to-1",       help="magic state factory preset")
    parser.add_argument("--factory-dims",      nargs=3, type=int, metavar=("I", "J", "K"),               help="custom factory footprint/interval (overrides --factory)")
    parser.add_argument("--die-dims",          nargs=2, type=int, metavar=("WIDTH", "ROWS"),             help="die constraint (ROWS 0 = grow on demand)")
    parser.add_argument("--factories",         type=int, default=1,                                      help="factory units (die mode)")
    parser.add_argument("--dump-ir",           action="store_true",                                      help="also write <out>.program.json")
    parser.add_argument("--budget",            type=int, default=600,                                    help="per-region SAT seconds")
    arguments = parser.parse_args()

    die_dims = (arguments.die_dims[0], arguments.die_dims[1] or None) if arguments.die_dims else None
    program = compile(
        arguments.qasm,
        cache_dir=arguments.cache_dir,
        max_gates=arguments.max_gates,
        side_ports=arguments.side_ports,
        factory=flash.FactorySpec("custom", *arguments.factory_dims) if arguments.factory_dims else arguments.factory,
        die_dims=die_dims,
        factories=arguments.factories,
        budget_s=arguments.budget,
    )

    gltf_path = Path(arguments.out)
    if gltf_path.parent != Path("."):
        gltf_path.parent.mkdir(parents=True, exist_ok=True)
    if arguments.dump_ir:
        gltf_path.with_suffix(".program.json").write_text(json.dumps(program.to_dict(), indent=1))
    flash.write_gltf(flash.build_layout(program), gltf_path)
    print(gltf_path)


if __name__ == "__main__":
    main()
