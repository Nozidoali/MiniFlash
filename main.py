"""The miniflash driver + CLI.

    python main.py benchmarks/algorithms/ghz8.qasm -o ghz8.gltf
    python main.py benchmarks/toffoli/*.qasm --solver placement > toffoli.csv
    python main.py --list benchmarks.txt --shapes 3

--solver names a step of the compiler's ladder (vanilla, dependency, routing,
spacing, placement, layout) or a development control (compact, staged, placed);
the other options adjust that configuration. Each circuit is parsed, compiled,
and verified before its CSV row is printed to stdout; a verification failure
aborts the run. compile_s covers parsing and solving, verify_s the independent
checker. With one circuit, ``-o`` renders the glTF scene and ``--json`` saves the
tile program. Progress goes to stderr.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

import miniflash as flash

FIELDS = ["circuit", "qubits", "pairs", "depth", "grid", "steps", "walk_steps", "volume", "compile_s", "verify_s"]


def shapes_option(text):
    return text if text in ("stats", "default") else int(text)


def read_input(path, hierarchical=False):
    """Read hierarchy JSON or QASM; a hierarchy can also supply the flat baseline."""
    if Path(path).suffix.lower() == ".json":
        hierarchy = flash.Hierarchy.load(path)
        return hierarchy.problem, hierarchy
    if hierarchical:
        raise ValueError("--hierarchical needs a hierarchy .json input")
    return flash.read_qasm(path), None


def compile(qasm_path, solver="layout", hierarchical=False, hierarchy_placement="weighted", **options):
    """Parse, solve and verify one circuit.

    :param qasm_path: str | Path to a .qasm or hierarchy .json file.
    :param solver: str, a key of ``miniflash.PRESETS``.
    :param options: Compiler options (shapes, extra, share, seed, factory, factories).
    :returns: (Problem, Program, Report).
    """
    problem, hierarchy = read_input(qasm_path, hierarchical)
    compiler = flash.make(solver, **options)
    program = (compiler.solve_hierarchy(hierarchy, hierarchy_placement) if hierarchical
               else compiler.solve(problem))
    report = flash.verify(problem, program, options.get("factory"), options.get("factories", 0))
    return problem, program, report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("circuits", nargs="*", help=".qasm or hierarchy .json files")
    ap.add_argument("--list", metavar="FILE", help="also compile the paths listed in FILE, one per line")
    ap.add_argument("--hierarchical", action="store_true", help="compile hierarchy JSON inputs bottom-up")
    ap.add_argument("--hierarchy-placement", choices=["weighted", "flat"], default="weighted",
                    help="hierarchical: place using call-count-weighted cells (default), or the full flat DAG")
    ap.add_argument("--solver", choices=list(flash.PRESETS), default="layout", help="ladder step or control (default: layout)")
    ap.add_argument("--shapes", type=shapes_option, default="default",
                    help="layout: 'stats' for the shape predicted from the DAG (the default) or the number of smallest-area shapes to compare")
    ap.add_argument("--extra", type=int, default=6, help="spacing: lane budget per partition beyond the site rows/cols")
    ap.add_argument("--share", type=float, default=4.0, help="routing: penalty per row or column a route newly loads")
    ap.add_argument("--seed", type=int, default=0, help="placement: anneal seed")
    ap.add_argument("--factory", type=flash.Factory.parse, metavar="HxWxT",
                    help="magic-state factory: H tiles deep, W along the chip side, one state per T steps. "
                         "Alone: one behind every magic tile. Omitted: unlimited supply")
    ap.add_argument("--factories", type=int, default=0,
                    help="F shared factories feeding the whole chip through delivery rings (default --factory 3x3x11); "
                         "-o draws them")
    ap.add_argument("-o", "--output", help="render the glTF scene (single circuit)")
    ap.add_argument("--windows", choices=["overlay", "coarse", "cells", "cells-inplace"],
                    help="with -o: draw the resynthesis windows over the program (overlay), carve them out and draw "
                         "each as an opaque box (coarse), or replace each by the cell lassynth finds for it, the "
                         "slabs moved down to their new depth (cells) or left where they were (cells-inplace). "
                         "cells need stim, z3, lassynth and kissat")
    ap.add_argument("--timeout", type=int, default=60, help="--windows cells: seconds per lassynth call")
    ap.add_argument("--json", help="save the tile program as JSON (single circuit)")
    ap.add_argument("--cut", choices=["dp", "greedy"], default="dp",
                    help="--windows: where to cut the slabs, by dynamic programming over a cost (default) "
                         "or greedily by estimated gain")
    a = ap.parse_args()
    paths = list(a.circuits) + (Path(a.list).read_text().split() if a.list else [])
    if not paths:
        ap.error("no circuits given")
    if (a.output or a.json) and len(paths) != 1:
        ap.error("-o/--json take a single circuit")
    if a.windows and not a.output:
        ap.error("--windows needs -o")
    solver = flash.make(a.solver, shapes=a.shapes, extra=a.extra, share=a.share, seed=a.seed,
                        factory=a.factory, factories=a.factories)
    if a.hierarchical and (not solver.routing or any(Path(p).suffix.lower() != ".json" for p in paths)):
        ap.error("--hierarchical requires JSON inputs, a routing/spacing/placement/layout solver")
    w = csv.DictWriter(sys.stdout, fieldnames=FIELDS)
    w.writeheader()
    for path in paths:
        t0 = time.perf_counter()
        problem, hierarchy = read_input(path, a.hierarchical)
        program = (solver.solve_hierarchy(hierarchy, a.hierarchy_placement) if a.hierarchical
                   else solver.solve(problem))
        t1 = time.perf_counter()
        rep = flash.verify(problem, program, a.factory, a.factories)
        t2 = time.perf_counter()
        w.writerow({"circuit": Path(path).stem, "qubits": problem.n_qubits, "pairs": len(problem.pairs), "depth": problem.depth,
                    "grid": f"{rep.height}x{rep.width}", "steps": rep.steps, "walk_steps": rep.walk_steps, "volume": rep.volume,
                    "compile_s": round(t1 - t0, 3), "verify_s": round(t2 - t1, 3)})
        sys.stdout.flush()
        print(f"{solver.name} {Path(path).stem}: {rep.steps} steps, volume {rep.volume}, {t1 - t0:.2f}s", file=sys.stderr, flush=True)
        if a.hierarchical:
            stats = solver.hierarchy_stats
            print(f"hierarchy: {stats['calls']} calls, {stats['cells']} distinct cells, "
                  f"{stats['routed_pairs']}/{stats['total_pairs']} pairs routed", file=sys.stderr, flush=True)
        if a.json:
            program.save(a.json)
        if a.output:
            supply = None
            if a.factories:
                supply = flash.plan(program, a.factories, a.factory or flash.Factory())
                for conflict in supply.conflicts:
                    print(f"supply conflict: {conflict}", file=sys.stderr)
            overlays, carve = (), False
            if a.windows:
                from miniflash.resynthesis import boxes, extract_dp, extract_greedy, write_cells
                picked = extract_dp(program, problem) if a.cut == "dp" else extract_greedy(program, problem, min_gain=0)
                carve = a.windows == "coarse"
                overlays = boxes(picked, alpha=1.0 if carve else 0.35)
                print(f"windows: {sum(len(s.windows) for s in picked)} in {len(picked)} slabs covering "
                      f"{sum(s.steps for s in picked)} of {rep.steps} steps", file=sys.stderr)
            if a.windows in ("cells", "cells-inplace"):
                from miniflash.resynthesis.synth import cells
                plan = cells(picked, program, problem, timeout=a.timeout,
                             report=lambda slab, c: print(f"cell {slab.start}-{slab.stop} {c.window.box.height}x{c.window.box.width}: "
                                                          f"{2 * c.window.steps} -> {c.depth} layers", file=sys.stderr, flush=True))
                layers = write_cells(program, plan, a.output, compact=a.windows == "cells")
                print(f"cells: {2 * rep.steps} -> {layers} layers, volume {2 * rep.volume} -> {layers * rep.height * rep.width} "
                      f"tile-layers", file=sys.stderr)
            else:
                flash.write_gltf(program, a.output, supply, overlays=overlays, carve=carve)


if __name__ == "__main__":
    main()
