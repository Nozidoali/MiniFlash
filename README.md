<p align="center">
  <img src="docs/logo.svg" width="120" alt="miniflash logo">
</p>

<h1 align="center">MiniFlash</h1>

<p align="center"><b>A <i>mini</i> and <i>fast</i> Clifford+T &rarr; lattice-surgery compiler.</b></p>

<p align="center">
  <a href="https://miniflash.readthedocs.io"><img src="https://readthedocs.org/projects/miniflash/badge/?version=latest" alt="documentation"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2626d9.svg" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-d92626.svg" alt="python 3.9+">
  <img src="https://img.shields.io/badge/pure-Python-26bf40.svg" alt="pure python">
</p>

MiniFlash turns a Clifford+T circuit (OpenQASM 2.0) into a
fault-tolerant **lattice-surgery layout**: the explicit 3-D spacetime
volume that a surface-code quantum computer would execute, with logical
qubits traced as pipes and every surgery, Hadamard and magic-state
injection placed. The result is a self-contained glTF scene — open it
in any 3-D viewer. Cell synthesis is
[LaSsynth](https://arxiv.org/abs/2404.18369) (Tan, Niu, Gidney — ISCA
2024). Read the [full documentation](https://miniflash.readthedocs.io).

## Quickstart

```bash
pip install -e .
python scripts/download_lassynth.py     # fetch LaSsynth from its Zenodo artifact (sha256-pinned)
python main.py benchmarks/algorithms/ghz8.qasm -o ghz8.gltf
# open in any glTF viewer, e.g. https://gltf-viewer.donmccurdy.com/
```

Partitioning is coarse-first: regions start at whole-circuit granularity
and split in place whenever a region exhausts its per-region SAT budget
(`--budget`, default 600 s). Compilation starts cold and warms the cell
cache as it goes; optionally pre-warm it from the published archive
(`cache-v1` release, sha256-pinned):

```bash
python scripts/download_cache.py
```

SAT solving uses
[kissat](https://github.com/arminbiere/kissat) when available — build it
and point `MINIFLASH_KISSAT_DIR` at the directory holding the `kissat`
binary — and falls back to z3 otherwise. `benchmarks/` ships 175
circuits to try (ghz/bv/dj, random Clifford, graph states, gf-mult,
Toffoli, ...).

As a library — the pipeline runs to the **Program IR**, and the
package's own backend renders it:

```python
import miniflash as flash

circuit = flash.parse("circuit.qasm")
pc = flash.partition(circuit)          # PartitionedCircuit: .regions / .events / .to_text() / .save_png()
floorplan, cells, channels = flash.synthesize(pc)
program = flash.elaborate(floorplan, cells, events=pc.events, channels=channels)

layout = flash.build_layout(program)                # check -> lower
flash.write_gltf(layout, "layout.gltf")

import json
json.dump(program.stats(), open("stats.json", "w"), indent=2)   # volumes, pauli frames, T corrections
```
