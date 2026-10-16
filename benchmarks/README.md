# Benchmark circuits

206 QASM circuits organized by family.

| folder | count | description |
|---|---|---|
| `algorithms/` | 41 | GHZ, BV, DJ, QFT, QPE, QAOA, Grover, Shor, HWB, Hamming, VQE, Wstate |
| `arithmetic/` | 17 | adders (rc/csla/csum/qcla/vbe/mod/ftcb), `qcla-com`, `qcla-mod`, `mod5-4` |
| `clifford/` | 22 | random Clifford: `-cxNN` batch (h/cx/s, by cx ratio) + `-dNN` batch (h/cx/cz, by depth); `clifford-complete-4`, `random-500` |
| `ftcb/` | 47 | FT compilation benchmarks (Fermi-Hubbard, Heisenberg, Ising, QFT, QPE, HHL, QSVT) |
| `gf-mult/` | 15 | GF(2^N) Galois-field multipliers |
| `graphstate/` | 12 | graphstate{4,8,16} chain/complete/dense/grid/linear/rand/ring |
| `synthlr/` | 5 | SynthesizeLR worst-case linear-reversible CNOT circuits (arXiv:2510.10967, DQI) |
| `t-factory/` | 8 | t-factory injection fixtures (chain/layer/pair/sandwich/smoke/clifford3) |
| `toffoli/` | 8 | Toffoli (`tof-N`) and Barenco-Toffoli |
| `qualtran/` | 23 | Qualtran bloqs with their call tree: adders, modular arithmetic up to exponentiation, GF(2^N) multiply and invert, comparison, QROM, Hubbard SELECT |

miniflash compiles Clifford+T: h/cx/x/s/sdg/t/tdg directly,
z/y/cz/swap/id/rz(k*pi/2)/ccx by auto-expansion. Circuits with gates
outside that set (arbitrary-angle rz etc.) raise at parse. Visualize a
circuit's partition with:

    PYTHONPATH=. python -c "import miniflash as flash; print(flash.partition(
        flash.parse('benchmarks/synthlr/synthlr-6.qasm')).to_text(color=True))"

## Hierarchical circuits

Each circuit in `qualtran/` comes twice: `<name>.qasm`, flat Clifford+T like every
other benchmark, and `<name>.json`, the same circuit as definitions that call one
another, one per bloq. Loading the JSON gives the pair DAG and, for every call, the
qubits it acts on and the pairs it spans:

    import miniflash as flash

    h = flash.Hierarchy.load("benchmarks/qualtran/modadd-4.json")
    h.problem                       # what the compiler takes
    h.calls("modadd")               # {'add': 1, 'addk': 1, 'addk_2': 1, 'lineardepthgreaterthan': 1}
    h.counts("add")                 # gates of one call, callees included
    h.defs["add"].preds()           # dependencies between the items of one body
    for node in h.tree.walk():      # one node per call
        node.name, node.qubits, node.pairs

Regenerate or add sizes with the system `python3`, which has qualtran:

    python3 scripts/qualtran_bloqs.py --list
    python3 scripts/qualtran_bloqs.py add-64 gf2mul-12

A measurement-based uncomputation (And†) keeps its conditional CZ as an ordinary
gate, so the circuits are a worst case and not a unitary; the JSON marks the
condition. Rotations that are not multiples of pi/4 are refused.
