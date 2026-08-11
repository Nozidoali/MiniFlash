"""The miniflash driver + CLI: ``python main.py circuit.qasm [-o out.gltf]``.

``compile()`` runs the package pipeline to the Program IR and is the
library entry point (``from main import compile``); ``main()`` prints the
volume stats, and renders the glTF scene (``write_gltf``) only when
``-o`` is given.
"""

import argparse
import json
from pathlib import Path

import miniflash as flash


def compile(qasm_path, cache_dir=".miniflash-cache", max_gates=16, side_ports=False, factory="15-to-1", die_dims=None, factories=1, budget_s=600, orientation=False, use_sat=False, fixed_columns=False):
    """Compile a circuit to the Program IR: parse -> partition -> synthesize -> elaborate.

    Partitioning is coarse-first: regions start at whole-circuit granularity
    (capped at 16 qubits) and split in place when a region exhausts its processing
    budget (16 -> 12 -> 8 -> 6 -> 4 -> 2).

    :param qasm_path: str | Path to a .qasm file.
    :param cache_dir: str, LaSsynth disk cache directory.
    :param max_gates: int, floor of the per-region gate cap.
    :param side_ports: bool, swap through cell side faces.
    :param factory: str preset name or FactorySpec.
    :param die_dims: (width, rows | None) or None for 1-D.
    :param factories: int, magic state factory units.
    :param budget_s: int, processing budget in seconds per region.
    :param use_sat: bool, SAT-solve uncached regions; False (default) serves
        cache hits and templates everything else.
    :param orientation: bool, lay deep cells down when that shortens their layer (1-D only).
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
    no_merge = set()
    while True:
        try:
            floorplan, cells, channels = flash.synthesize(partitioned, cache_dir=cache_dir, side_ports=side_ports, die_dims=die_dims, budget_s=budget_s, orientation=orientation, use_sat=use_sat, fixed_columns=fixed_columns, no_merge=frozenset(no_merge))
            break
        except RuntimeError as error:
            merge_keys = getattr(error, "merge_keys", None)
            if merge_keys is not None:
                no_merge.add(merge_keys)
                continue
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
    parser.add_argument("qasm",                                              help="input .qasm file")
    parser.add_argument("-o", "--out",                                       help="write the glTF scene to this path (default: stats only, no glTF)")
    parser.add_argument("--factory",     default="15-to-1",                  help=f"magic state factory: {' | '.join(sorted(flash.FACTORIES))} or custom IxJxK dims (e.g. 6x2x11)")
    parser.add_argument("--die",         type=int, metavar="WIDTH",          help="die width constraint (rows grow on demand)")
    parser.add_argument("--factories",   type=int, default=1,                help="factory units (die mode)")
    parser.add_argument("--sat",         nargs="?", type=int, const=600, metavar="SECONDS", help="SAT-solve uncached regions, per-region budget in seconds (default: cached cells + templates only)")
    parser.add_argument("--side-ports",  action="store_true",                help="swap through cell side faces (1-D only)")
    parser.add_argument("--orientation", action="store_true",                help="lay deep cells down (1-D only)")
    parser.add_argument("--cache-dir",   default=".miniflash-cache",         help="cell cache directory")
    parser.add_argument("--dump-ir",     action="store_true",                help="also write <name>.program.json")
    arguments = parser.parse_args()

    factory = arguments.factory
    if factory not in flash.FACTORIES:
        try:
            factory = flash.FactorySpec("custom", *(int(dim) for dim in factory.split("x")))
        except (TypeError, ValueError):
            parser.error(f"--factory: {factory!r} is neither a preset ({', '.join(sorted(flash.FACTORIES))}) nor IxJxK dims")
    program = compile(
        arguments.qasm,
        cache_dir=arguments.cache_dir,
        side_ports=arguments.side_ports,
        factory=factory,
        die_dims=(arguments.die, None) if arguments.die else None,
        factories=arguments.factories,
        budget_s=arguments.sat if arguments.sat else 600,
        orientation=arguments.orientation,
        use_sat=arguments.sat is not None,
    )

    if arguments.dump_ir:
        ir_path = (Path(arguments.out) if arguments.out else Path(Path(arguments.qasm).name)).with_suffix(".program.json")
        if ir_path.parent != Path("."):
            ir_path.parent.mkdir(parents=True, exist_ok=True)
        ir_path.write_text(json.dumps(program.to_dict(), indent=1))
    if arguments.out:
        gltf_path = Path(arguments.out)
        if gltf_path.parent != Path("."):
            gltf_path.parent.mkdir(parents=True, exist_ok=True)
        flash.write_gltf(program, gltf_path)
        print(gltf_path)
    else:
        stats = program.stats()
        line = f"miniflash: {Path(arguments.qasm).stem} cube_envelope={stats['cube_envelope']} volume={stats['volume']}"
        if stats.get("t_count") is not None:
            line += f" t_count={stats['t_count']} wait_levels={stats['wait_levels']}"
        print(line)


if __name__ == "__main__":
    main()
