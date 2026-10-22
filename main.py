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


def compile(qasm_path, cache_dir=".miniflash-cache", max_gates=16, side_ports=False, factory="15-to-1", die_dims=None, factories=1, budget_s=600, orientation=False, use_sat=False, fixed_columns=False, factory_mode=None):
    """Compile a circuit to the Program IR: parse -> partition -> synthesize -> elaborate.

    Partitioning is coarse-first: regions start at whole-circuit granularity
    (capped at 16 qubits) and split in place when a region exhausts its processing
    budget (16 -> 12 -> 8 -> 6 -> 4 -> 2).

    :param qasm_path: str | Path to a .qasm file.
    :param cache_dir: str, LaSsynth disk cache directory.
    :param max_gates: int, floor of the per-region gate cap.
    :param side_ports: must be False for the physical-layout backend.
    :param factory: str preset name or FactorySpec.
    :param die_dims: initial synthesis (width, rows | None), or None for 1-D;
        the final physical rectangle may grow beyond these dimensions.
    :param factories: positive int, reserved magic state factory units.
    :param factory_mode: in_place, backside_vertical or backside_horizontal;
        None selects in_place for factories <= 2, otherwise backside_horizontal.
    :param budget_s: int, processing budget in seconds per region.
    :param use_sat: bool, SAT-solve uncached regions; False (default) serves
        cache hits and templates everything else.
    :param orientation: must be False for the physical-layout backend.
    :param fixed_columns: must be False for the physical-layout backend.
    :returns: Program.
    """
    from miniflash.physical import resolve_mode
    factory_mode = resolve_mode(factory_mode, factories)
    if orientation or side_ports or fixed_columns:
        raise ValueError("physical factory layouts require upright cells and ordinary ports (orientation/side_ports/fixed_columns=False)")
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

    return flash.elaborate(floorplan, cells, events=events, channels=channels, factory=factory_spec, factories=factories, factory_mode=factory_mode)


def main():
    """CLI entry point: compile to the Program IR, then write the glTF scene.

    :returns: None; prints the glTF path.
    """
    parser = argparse.ArgumentParser(description="Compile a Clifford+T OpenQASM 2.0 circuit into a lattice-surgery layout (glTF 2.0)")
    parser.add_argument("qasm",                                              help="input .qasm file")
    parser.add_argument("-o", "--out",                                       help="write the glTF scene to this path (default: stats only, no glTF)")
    parser.add_argument("--factory-mode", choices=("in_place", "backside_vertical", "backside_horizontal"), help="default: in_place for F<=2, otherwise backside_horizontal (4-row corridor)")
    parser.add_argument("--factory",     default="15-to-1",                  help=f"magic state factory: {' | '.join(sorted(flash.FACTORIES))} or custom IxJxK dims (e.g. 6x2x11)")
    parser.add_argument("--die",         type=int, metavar="WIDTH",          help="initial cell synthesis width (final physical rectangle may grow)")
    parser.add_argument("--factories",   type=int, default=1,                help="reserved factory units (positive integer; default 1)")
    parser.add_argument("--sat",         nargs="?", type=int, const=600, metavar="SECONDS", help="SAT-solve uncached regions, per-region budget in seconds (default: cached cells + templates only)")
    parser.add_argument("--side-ports",  action="store_true",                help="unsupported by the physical-layout backend")
    parser.add_argument("--orientation", action="store_true",                help="unsupported by the physical-layout backend")
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
        factory_mode=arguments.factory_mode,
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
        line = f"miniflash: {Path(arguments.qasm).stem} factory_mode={stats['factory_mode']} cube_envelope={stats['cube_envelope']} volume={stats['volume']}"
        if stats.get("t_count") is not None:
            line += f" t_count={stats['t_count']} wait_levels={stats['wait_levels']}"
        print(line)


if __name__ == "__main__":
    main()
