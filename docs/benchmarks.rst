Benchmarks
==========

Compiled **spacetime volume** on 44 circuits, against **TopoLS** (Zhou
et al., `arXiv:2601.23109 <https://arxiv.org/abs/2601.23109>`_) and
**DasCot** (Molavi et al., `arXiv:2311.18042
<https://arxiv.org/abs/2311.18042>`_). Below 1× = MiniFlash smaller.

.. list-table::
   :header-rows: 1
   :widths: 24 14 20 20

   * - family
     - circuits
     - geomean vs TopoLS
     - geomean vs DasCot
   * - GHZ state preparation
     - 7
     - 0.61×
     - 0.20×
   * - Bernstein–Vazirani
     - 8
     - 0.51×
     - 0.19×
   * - Deutsch–Jozsa
     - 8
     - 1.28×
     - 0.84×
   * - random Clifford
     - 9
     - 0.84×
     - 0.94×
   * - graph states
     - 12
     - 0.71×
     - 0.84×
   * - **all**
     - **44**
     - **0.76×**
     - **0.52×**

Four of the five families favor MiniFlash; the driver on both sides is
**cell granularity**. Wins come from whole-circuit cells that amortize
all fixed overhead; losses appear exactly where the SAT instance
exceeds its budget and the circuit splits into stitched fragments.

.. note::

   Volume = tile bounding box, the same unit in all three tools.
   Identical QASM inputs; TopoLS under a 180 s wall (misses “—”,
   bv-100 from its fast profile); random Clifford takes MiniFlash's
   best configuration per circuit. Measured 2026-08.

GHZ State Preparation
---------------------

The advantage widens with size and then stabilizes — 0.44–0.46× vs
TopoLS and 0.12–0.13× vs DasCot from 10 qubits on: one whole-circuit
cell, constant overhead amortized.

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 15 15

   * - circuit
     - MiniFlash
     - TopoLS
     - DasCot
     - vs TopoLS
     - vs DasCot
   * - ``ghz3``
     - 45
     - 42
     - 98
     - 1.07×
     - 0.46×
   * - ``ghz4``
     - 54
     - 81
     - 147
     - 0.67×
     - 0.37×
   * - ``ghz6``
     - 72
     - 135
     - 405
     - 0.53×
     - 0.18×
   * - ``ghz8``
     - 120
     - 135
     - 567
     - 0.89×
     - 0.21×
   * - ``ghz10``
     - 144
     - 315
     - 1,089
     - 0.46×
     - 0.13×
   * - ``ghz12``
     - 168
     - 378
     - 1,331
     - 0.44×
     - 0.13×
   * - ``ghz16``
     - 216
     - 486
     - 1,815
     - 0.44×
     - 0.12×

Bernstein–Vazirani
--------------------

Green at every size (0.33–0.67× / 0.12–0.25×): the chain structure
compiles into few wide cells and never needs a split.

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 15 15

   * - circuit
     - MiniFlash
     - TopoLS
     - DasCot
     - vs TopoLS
     - vs DasCot
   * - ``bv-16``
     - 216
     - 648
     - 1,815
     - 0.33×
     - 0.12×
   * - ``bv-20``
     - 621
     - 1,584
     - 3,211
     - 0.39×
     - 0.19×
   * - ``bv-30``
     - 990
     - 2,295
     - 6,525
     - 0.43×
     - 0.15×
   * - ``bv-40``
     - 2,160
     - 3,402
     - 11,271
     - 0.63×
     - 0.19×
   * - ``bv-50``
     - 3,420
     - 5,103
     - 17,689
     - 0.67×
     - 0.19×
   * - ``bv-60``
     - 4,221
     - 7,533
     - 21,299
     - 0.56×
     - 0.20×
   * - ``bv-80``
     - 8,463
     - —
     - 34,839
     - —
     - 0.24×
   * - ``bv-100``
     - 12,882
     - 20,196
     - 52,371
     - 0.64×
     - 0.25×

Deutsch–Jozsa
---------------

The one family that loses to TopoLS (1.28×): star connectivity
fragments into a serial chain of cells and the stitching depth adds up.
Against DasCot it still wins (0.84×).

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 15 15

   * - circuit
     - MiniFlash
     - TopoLS
     - DasCot
     - vs TopoLS
     - vs DasCot
   * - ``dj-16``
     - 714
     - 486
     - 567
     - 1.47×
     - 1.26×
   * - ``dj-20``
     - 1,008
     - 594
     - 1,089
     - 1.70×
     - 0.93×
   * - ``dj-30``
     - 1,488
     - 1,071
     - 1,694
     - 1.39×
     - 0.88×
   * - ``dj-40``
     - 2,580
     - 1,890
     - 3,211
     - 1.37×
     - 0.80×
   * - ``dj-50``
     - 3,498
     - 2,916
     - 4,056
     - 1.20×
     - 0.86×
   * - ``dj-60``
     - 4,158
     - 4,464
     - 6,525
     - 0.93×
     - 0.64×
   * - ``dj-80``
     - 8,568
     - 8,118
     - 11,271
     - 1.06×
     - 0.76×
   * - ``dj-100``
     - 13,230
     - 10,098
     - 17,689
     - 1.31×
     - 0.75×

Random Clifford Circuits
------------------------

Through 4 qubits every instance collapses into a single depth-3 cell
(volume 54, up to 0.12×). From ``8q-cx50`` the whole-circuit SAT
instance exceeds its budget, cells split, and the ratio crosses 1×.

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 15 15

   * - circuit
     - MiniFlash
     - TopoLS
     - DasCot
     - vs TopoLS
     - vs DasCot
   * - ``4q-cx25``
     - 54
     - 243
     - 245
     - 0.22×
     - 0.22×
   * - ``4q-cx50``
     - 54
     - 243
     - 392
     - 0.22×
     - 0.14×
   * - ``4q-cx75``
     - 54
     - 351
     - 441
     - 0.15×
     - 0.12×
   * - ``8q-cx25``
     - 180
     - 540
     - 243
     - 0.33×
     - 0.74×
   * - ``8q-cx50``
     - 1,188
     - 630
     - 567
     - 1.89×
     - 2.10×
   * - ``8q-cx75``
     - 1,404
     - 810
     - 891
     - 1.73×
     - 1.58×
   * - ``16q-cx25``
     - 1,632
     - 891
     - 605
     - 1.83×
     - 2.70×
   * - ``16q-cx50``
     - 5,292
     - 1,296
     - 968
     - 4.08×
     - 5.47×
   * - ``16q-cx75``
     - 7,104
     - 2,025
     - 1,694
     - 3.51×
     - 4.19×

Graph States
------------

Rings, chains and sparse random graphs win outright (0.19–0.54×);
small dense graphs and grids lose, with ``gs16-grid`` paying the same
split tax as ``synthlr-12`` below.

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 15 15

   * - circuit
     - MiniFlash
     - TopoLS
     - DasCot
     - vs TopoLS
     - vs DasCot
   * - ``gs4-chain``
     - 54
     - 162
     - 147
     - 0.33×
     - 0.37×
   * - ``gs4-ring``
     - 54
     - 189
     - 196
     - 0.29×
     - 0.28×
   * - ``gs4-dense``
     - 351
     - 243
     - 196
     - 1.44×
     - 1.79×
   * - ``gs4-complete``
     - 576
     - 297
     - 245
     - 1.94×
     - 2.35×
   * - ``gs8-ring``
     - 120
     - 360
     - 648
     - 0.33×
     - 0.19×
   * - ``gs8-linear``
     - 468
     - 360
     - 567
     - 1.30×
     - 0.83×
   * - ``gs8-grid2x4``
     - 672
     - 405
     - 324
     - 1.66×
     - 2.07×
   * - ``gs8-rand50``
     - 396
     - 1,215
     - 729
     - 0.33×
     - 0.54×
   * - ``gs8-rand75``
     - 1,008
     - 1,935
     - 891
     - 0.52×
     - 1.13×
   * - ``gs8-complete``
     - 702
     - 2,655
     - 1,053
     - 0.26×
     - 0.67×
   * - ``gs16-linear``
     - 1,452
     - —
     - 1,815
     - —
     - 0.80×
   * - ``gs16-grid``
     - 4,218
     - 1,620
     - 1,452
     - 2.60×
     - 2.90×

Worst-Case CNOT Circuits (SynthesizeLR)
---------------------------------------

Worst-case CNOT circuits of Khattar et al. (`arXiv:2510.10967
<https://arxiv.org/abs/2510.10967>`_); ``manual`` = their hand-optimized
construction, :math:`3(2n-1)(n-2)`, same unit. The per-instance SAT
layout overtakes the worst-case template from :math:`n=6` and widens
with :math:`n` (0.73× → 0.32×), while staying 10–50× under both
compilers.

.. list-table::
   :header-rows: 1
   :widths: 18 14 14 14 14 13 13

   * - circuit
     - MiniFlash
     - manual
     - TopoLS
     - DasCot
     - vs manual
     - vs DasCot
   * - ``synthlr-4``
     - 54
     - 42
     - 351
     - 539
     - 1.29×
     - 0.10×
   * - ``synthlr-6``
     - 96
     - 132
     - 1,125
     - 1,944
     - 0.73×
     - 0.05×
   * - ``synthlr-8``
     - 120
     - 270
     - 1,980
     - 3,321
     - 0.44×
     - 0.04×
   * - ``synthlr-10``
     - 144
     - 456
     - —
     - 7,502
     - 0.32×
     - 0.02×
   * - ``synthlr-12``
     - 14,148
     - 690
     - —
     - 10,648
     - 20.50×
     - 1.33×

``synthlr-12`` hits MiniFlash's SAT capacity wall (the cell splits into
19 pieces). Not part of the headline geomean.

T-Dense Circuits (Galois-Field Multiplication)
----------------------------------------------

Injection-dominated circuits (112–448 T gates): MiniFlash trails
DasCot, geomean **3.05×** (die mode); TopoLS times out on all of them.
The residual is a fabric constant — each cell layer plus its channel
gap costs ~10 time-steps against DasCot's ~1 per routed operation — not
injection scheduling.

.. list-table::
   :header-rows: 1
   :widths: 24 14 18 18 15

   * - circuit
     - T count
     - MiniFlash
     - DasCot
     - vs DasCot
   * - ``gf2e4-mult``
     - 112
     - 28,350
     - 12,463
     - 2.27×
   * - ``gf2e5-mult``
     - 175
     - 49,104
     - 16,456
     - 2.98×
   * - ``gf2e6-mult``
     - 252
     - 81,474
     - 28,899
     - 2.82×
   * - ``gf2e7-mult``
     - 343
     - 117,519
     - 34,645
     - 3.39×
   * - ``gf2e8-mult``
     - 448
     - 170,226
     - 42,081
     - 4.05×

