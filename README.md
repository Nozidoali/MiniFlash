# MiniFlash

A small Clifford+T → lattice-surgery compiler. MiniFlash turns an OpenQASM 2.0
circuit into a **tile program**: which tiles hold qubits, magic states and routes,
and which tiles merge at each step. It minimizes spacetime volume (height × width
× steps, walks included), checks every program with an independent verifier, and
renders it as a glTF scene.

## Quickstart

```bash
pip install -e '.[test]'
python -m pytest -q tests
python main.py benchmarks/algorithms/ghz8.qasm -o ghz8.gltf
```

Open the `.gltf` in any viewer, e.g. <https://gltf-viewer.donmccurdy.com/>. Time
runs upward, two layers per step. Qubits are red pillars. A CNOT splits at its
first turn, where an ancilla climbs: the half from the control's Z side sits on the
lower layer (blue), the half into the target's X side on the upper layer (red).
T merges end at green magic states; walks between partitions are orange.

Several circuits print a CSV, one verified row each:

```bash
python main.py benchmarks/toffoli/*.qasm --solver placement > toffoli.csv
```

As a library:

```python
import miniflash as flash

problem = flash.read_qasm("benchmarks/toffoli/tof-3.qasm")
program = flash.make("layout").solve(problem)
report = flash.verify(problem, program)
flash.write_gltf(program, "tof3.gltf")
```

## Options

- `--solver`: a step of the compiler's ladder, `vanilla`, `dependency`, `routing`,
  `spacing`, `placement`, `layout` (default), or a development control (`compact`,
  `staged`, `placed`). `--shapes N` compares the N smallest site shapes.
- `--factory HxWxT`: magic-state factory, H tiles deep, W wide, one state every T
  steps. Alone, one factory sits behind every magic tile.
- `--factories F`: F shared factories feed the whole chip through delivery rings
  around it; `-o` draws them.
- `--json FILE`: save the tile program.

Without `--factory`, magic supply is unlimited. Single-qubit Cliffords are free.

`benchmarks/` holds 206 circuits (GHZ, BV, DJ, QFT, random Clifford, graph states,
Galois-field multipliers, Toffoli, ...). Those in `benchmarks/qualtran/` also carry
the call tree of the Qualtran bloq they come from; see `benchmarks/README.md`.

## Hierarchical compilation

Compile repeated subroutines once on a common floorplan, then compose their
routes before partitioning and compaction:

```bash
python main.py benchmarks/qualtran/modexp-3.json --hierarchical --json out/modexp.json
# The same input compiled as a flat DAG:
python main.py benchmarks/qualtran/modexp-3.json
```

```python
hierarchy = flash.Hierarchy.load("benchmarks/qualtran/modexp-3.json")
compiler = flash.make("layout")
program = compiler.solve_hierarchy(hierarchy)
report = flash.verify(hierarchy.problem, program)
print(compiler.hierarchy_stats)  # calls, distinct cells, and pairs actually routed
```

Compilation follows the call tree bottom-up, with no size cutoff. It routes each
distinct leaf (and direct gate run between children), composes each parent from
its cached children, and caches the parent for reuse up to the root. Leaves
match exact pair sequences on the same global qubits; parents also preserve
child boundaries. Unused program qubits remain routing obstacles.

Child steps keep their order and grouping, but calls may overlap once their
qubits are available. The composer reroutes conflicts or waits. The root enforces
dependencies and magic-state supply across all nested calls, then the usual
partitioning, compaction, and walk passes run. The usual `--factory`,
`--factories`, `--shapes`, rendering, and output options apply; hierarchical
compilation requires a solver with routing enabled.

Placement uses each distinct leaf's ASAP layers weighted by its expanded call
count, so ancestor templates do not double-count gates. This preserves interaction
frequencies while approximating overlap between calls. `--hierarchy-placement flat`
uses the full DAG's placement instead, for controlled comparisons. The hierarchy
loader and output program still expand all calls; cached parent schedules also
store expanded routes. Reuse saves repeated optimization work, not that memory cost.

`hierarchy_stats` reports all cached `cells`, split into `leaf_cells` and
`composed_cells`. `calls` counts invocations across all levels, including the root
and direct gate runs; `leaf_calls` counts just the leaves. `routed_pairs` counts
pairs sent to the leaf router once per distinct leaf. Parent composition may
reroute conflicts, and its time is reported separately as `compose_s`.

Run a verified comparison of eight Qualtran circuits (flat, weighted cell
placement, and cell reuse with flat placement):

```bash
OPENBLAS_NUM_THREADS=1 python scripts/benchmark_hierarchy.py --output out/hierarchy.csv
```

## License

MIT
