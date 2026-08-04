# Benchmark circuits

175 QASM circuits organized by family.

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

miniflash compiles Clifford+T: h/cx/x/s/sdg/t/tdg directly,
z/y/cz/swap/id/rz(k*pi/2)/ccx by auto-expansion. Circuits with gates
outside that set (arbitrary-angle rz etc.) raise at parse. Visualize a
circuit's partition with:

    PYTHONPATH=. python -c "import miniflash as flash; print(flash.partition(
        flash.parse('benchmarks/synthlr/synthlr-6.qasm')).to_text(color=True))"
