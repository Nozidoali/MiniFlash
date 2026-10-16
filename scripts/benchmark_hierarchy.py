"""Compare flat compilation and cell reuse on the same Qualtran inputs.

Run with one BLAS thread for reproducible CPU timing, e.g.:
    OPENBLAS_NUM_THREADS=1 python scripts/benchmark_hierarchy.py --output out/hierarchy.csv

Each row passes the independent verifier. `solve_s` excludes JSON loading and
verification; `load_s` records eager hierarchy expansion separately. The default
eight cases cover arithmetic, lookup, and simulation with differing reuse.
"""
import argparse
import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from miniflash import Hierarchy, make, verify

DEFAULTS = ["modexp-3", "modmul-4", "gf2inv-4", "modadd-8", "add-16", "hubbard-2", "qrom-6", "gf2mul-8"]
FIELDS = ["circuit", "seed", "mode", "pairs", "cells", "calls", "leaf_cells", "composed_cells", "leaf_calls", "routed_pairs", "volume", "steps", "walks",
          "load_s", "solve_s", "verify_s", "extract_s", "map_s", "route_s", "compose_s", "windows_s", "compact_s"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("circuits", nargs="*", help="benchmark stems or JSON paths")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0])
    ap.add_argument("--modes", nargs="+", choices=["flat", "weighted", "hierarchy-flat"], default=["flat", "weighted", "hierarchy-flat"])
    ap.add_argument("--output", type=Path, required=True)
    a = ap.parse_args()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for name in a.circuits or DEFAULTS:
            path = Path(name) if name.endswith(".json") else ROOT / "benchmarks/qualtran" / f"{name}.json"
            started = time.perf_counter()
            hierarchy = Hierarchy.load(path)
            load_s = time.perf_counter() - started
            for seed in a.seeds:
                for mode in a.modes:
                    compiler = make("layout", seed=seed)
                    started = time.perf_counter()
                    program = (compiler.solve(hierarchy.problem) if mode == "flat" else
                               compiler.solve_hierarchy(hierarchy, "weighted" if mode == "weighted" else "flat"))
                    solved = time.perf_counter()
                    report = verify(hierarchy.problem, program)
                    checked = time.perf_counter()
                    stats = compiler.hierarchy_stats
                    profile = compiler.profile[0]  # default layout uses one predicted shape
                    row = dict(circuit=path.stem, seed=seed, mode=mode, pairs=len(hierarchy.problem.pairs),
                               cells=stats.get("cells", 0), calls=stats.get("calls", 0),
                               **{key: stats.get(key, 0) for key in ("leaf_cells", "composed_cells", "leaf_calls")},
                               routed_pairs=stats.get("routed_pairs", len(hierarchy.problem.pairs)),
                               volume=report.volume, steps=report.steps, walks=report.walk_steps,
                               load_s=load_s, solve_s=solved - started, verify_s=checked - solved,
                               extract_s=stats.get("extract_s", 0),
                               **{key: profile[key] for key in ("map_s", "route_s", "compose_s", "windows_s", "compact_s")})
                    writer.writerow(row)
                    out.flush()
                    print(f"{path.stem} seed={seed} {mode}: {solved - started:.3f}s, volume={report.volume}", flush=True)


if __name__ == "__main__":
    main()
