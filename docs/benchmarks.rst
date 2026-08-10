Benchmarks
==========

Compiled **spacetime volume** on 44 circuits, against **TopoLS** (Zhou
et al., `arXiv:2601.23109 <https://arxiv.org/abs/2601.23109>`_) and
**DasCot** (Molavi et al., `arXiv:2311.18042
<https://arxiv.org/abs/2311.18042>`_). Below 1× = MiniFlash smaller.

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16

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
     - 0.46×
     - 0.17×
   * - Deutsch–Jozsa
     - 8
     - 1.08×
     - 0.71×
   * - random Clifford
     - 9
     - 0.78×
     - 0.87×
   * - graph states
     - 12
     - 0.74×
     - 0.88×
   * - **all**
     - **44**
     - **0.72×**
     - **0.49×**

Four of the five families favor MiniFlash; the driver on both sides is
**cell granularity**. Wins come from whole-circuit cells that amortize
all fixed overhead; losses appear exactly where the SAT instance
exceeds its budget and the circuit splits into stitched fragments.

.. note::

   Volume = tile bounding box, the same unit in all three tools.
   Identical QASM inputs; TopoLS under a 180 s wall (misses "—",
   bv-100 from its fast profile). MiniFlash runs use ``--sat`` with a
   warm cell cache and the default 600 s per-region budget.
   Measured 2026-08.

GHZ State Preparation
---------------------

The advantage widens with size and then stabilizes — one whole-circuit
cell, constant overhead amortized.

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 16 16

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
------------------

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 16 16

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
     - 552
     - 1,584
     - 3,211
     - 0.35×
     - 0.17×
   * - ``bv-30``
     - 891
     - 2,295
     - 6,525
     - 0.39×
     - 0.14×
   * - ``bv-40``
     - 1,890
     - 3,402
     - 11,271
     - 0.56×
     - 0.17×
   * - ``bv-50``
     - 3,078
     - 5,103
     - 17,689
     - 0.60×
     - 0.17×
   * - ``bv-60``
     - 3,819
     - 7,533
     - 21,299
     - 0.51×
     - 0.18×
   * - ``bv-80``
     - 7,644
     - —
     - 34,839
     - —
     - 0.22×
   * - ``bv-100``
     - 11,526
     - 20,196
     - 52,371
     - 0.57×
     - 0.22×

Deutsch–Jozsa
-------------

The one family that loses to TopoLS (1.08×): star connectivity
fragments into a serial chain of cells and the stitching depth adds up.
Against DasCot it wins (0.71×).

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 16 16

   * - circuit
     - MiniFlash
     - TopoLS
     - DasCot
     - vs TopoLS
     - vs DasCot
   * - ``dj-16``
     - 612
     - 486
     - 567
     - 1.26×
     - 1.08×
   * - ``dj-20``
     - 882
     - 594
     - 1,089
     - 1.48×
     - 0.81×
   * - ``dj-30``
     - 1,302
     - 1,071
     - 1,694
     - 1.22×
     - 0.77×
   * - ``dj-40``
     - 2,064
     - 1,890
     - 3,211
     - 1.09×
     - 0.64×
   * - ``dj-50``
     - 2,862
     - 2,916
     - 4,056
     - 0.98×
     - 0.71×
   * - ``dj-60``
     - 3,402
     - 4,464
     - 6,525
     - 0.76×
     - 0.52×
   * - ``dj-80``
     - 7,056
     - 8,118
     - 11,271
     - 0.87×
     - 0.63×
   * - ``dj-100``
     - 11,340
     - 10,098
     - 17,689
     - 1.12×
     - 0.64×

Random Clifford
---------------

Best configuration per circuit (``--orientation`` / ``--side-ports``
sweeps).

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 16 16

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
     - 144
     - 540
     - 243
     - 0.27×
     - 0.59×
   * - ``8q-cx50``
     - 855
     - 630
     - 567
     - 1.36×
     - 1.51×
   * - ``8q-cx75``
     - 1,380
     - 810
     - 891
     - 1.70×
     - 1.55×
   * - ``16q-cx25``
     - 1,344
     - 891
     - 605
     - 1.51×
     - 2.22×
   * - ``16q-cx50``
     - 6,105
     - 1,296
     - 968
     - 4.71×
     - 6.31×
   * - ``16q-cx75``
     - 6,600
     - 2,025
     - 1,694
     - 3.26×
     - 3.90×

Graph States
------------

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 16 16

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
     - 360
     - 243
     - 196
     - 1.48×
     - 1.84×
   * - ``gs4-complete``
     - 468
     - 297
     - 245
     - 1.58×
     - 1.91×
   * - ``gs8-ring``
     - 429
     - 360
     - 648
     - 1.19×
     - 0.66×
   * - ``gs8-linear``
     - 396
     - 360
     - 567
     - 1.10×
     - 0.70×
   * - ``gs8-grid2x4``
     - 507
     - 405
     - 324
     - 1.25×
     - 1.56×
   * - ``gs8-rand50``
     - 363
     - 1,215
     - 729
     - 0.30×
     - 0.50×
   * - ``gs8-rand75``
     - 858
     - 1,935
     - 891
     - 0.44×
     - 0.96×
   * - ``gs8-complete``
     - 594
     - 2,655
     - 1,053
     - 0.22×
     - 0.56×
   * - ``gs16-linear``
     - 1,452
     - —
     - 1,815
     - —
     - 0.80×
   * - ``gs16-grid``
     - 5,586
     - 1,620
     - 1,452
     - 3.45×
     - 3.85×

Sparse Long-Range Circuits (SynthLR)
------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 16 16 16

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
     - 168
     - 690
     - —
     - 10,648
     - 0.24×
     - 0.02×

T-Dense Circuits (Galois-Field Multiplication)
----------------------------------------------

Injection-dominated circuits (112–448 T gates): MiniFlash trails
DasCot, geomean **2.67×** (die mode, ``t-cultivation``,
gap-riding injections); TopoLS times out on all of them. The residual
is the per-layer transition toll (channel gap plus injection levels)
against DasCot's ~1 step per routed operation — see the fixed-column
work for the path below 1×.

.. list-table::
   :header-rows: 1
   :widths: 22 16 16 16 16

   * - circuit
     - T count
     - MiniFlash
     - DasCot
     - vs DasCot
   * - ``gf2e4-mult``
     - 112
     - 23,400
     - 12,463
     - 1.88×
   * - ``gf2e5-mult``
     - 175
     - 43,659
     - 16,456
     - 2.65×
   * - ``gf2e6-mult``
     - 252
     - 70,971
     - 28,899
     - 2.46×
   * - ``gf2e7-mult``
     - 343
     - 105,252
     - 34,645
     - 3.04×
   * - ``gf2e8-mult``
     - 448
     - 153,558
     - 42,081
     - 3.65×
