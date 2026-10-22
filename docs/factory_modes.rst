Physical factory layouts
========================

The compiler selects one physical factory layout for the whole program.
With ``--factory-mode`` omitted, a reserved factory count F <= 2 selects
``in_place``; F > 2 selects ``backside_horizontal``. The default factory
count remains 1. There is no ``auto`` mode or per-injection-group switch
between side and backside delivery.

The three explicit choices override this default policy:

.. list-table::
   :header-rows: 1

   * - Mode
     - Placement
     - Enabled factories
   * - ``in_place``
     - Left and right of the reserved compute band
     - min(2, F); all F sites still reserve area
   * - ``backside_vertical``
     - One vertical bank to the right of the compute band
     - F
   * - ``backside_horizontal``
     - One horizontal bank below the preserved compute footprint,
       separated by exactly four empty patch rows
     - F

Examples::

   python main.py circuit.qasm --factories 2
   python main.py circuit.qasm --factories 6
   python main.py circuit.qasm --factories 6 --factory-mode in_place
   python main.py circuit.qasm --factories 6 --factory-mode backside_vertical
   python main.py circuit.qasm --factories 6 --factory-mode backside_horizontal

The library entry points ``main.compile`` and ``miniflash.elaborate`` accept
``factory_mode=None`` for the same F-based default. Program serialization
records the resolved explicit mode, not a deferred default. A T-free program
still records the mode selected from the requested F but reserves no factories.

Model and accounting
--------------------

Each reserved factory has a permanent site and a one-state output buffer.
The ``15-to-1`` preset occupies 6 by 2 patches and produces a state every
11 logical steps. Factories start cold, pause when their output buffer is
full, and begin a new production interval on consumption. In-place sites
beyond the first two are disabled but remain in the allocated rectangle.

Data movement precedes boundary injections. Checked planar paths connect
factory buffers to J-adjacent ancillas, avoiding unrelated data, factories
and other buffers. Conflicting paths execute in separate rounds. The whole
ancilla path is occupied for a two-step injection gadget; this is not a
per-tile hopping-latency model. All outputs retain their left-facing ports.

The checker replays cell boundaries, data movement, routes, event coverage
and factory tokens. Statistics and glTF rendering consume the same stored
plan. ``patch_cell_slices = allocated_area_patches * runtime_steps`` includes
factories, buffers, routing space, cold start and final injection completion.
``volume`` is eight times that value in native coordinates. Factory area must
not be added again. ``compute_volume`` is not emitted.

Compatibility
-------------

This replaces main's legacy mixed side/backside lowering and its envelope
accounting. Numerical volumes are not directly interchangeable with the old
model. Legacy Program JSON without an explicit physical plan is rejected;
recompile its source QASM. Experimental names ``auto``, ``backside`` and
``in_place_joint`` are not accepted; use the three names above.

This backend requires upright cells and ordinary K-face ports. The existing
``orientation``, ``side_ports`` and ``fixed_columns`` options are rejected.
``die_dims`` still constrains initial cell synthesis, not the final allocated
rectangle. Cells are translated and their connections rerouted; the layout
does not change logical dependencies, resynthesize Clifford cells, or move T
events across scheduled Clifford boundaries. The parser's existing T hoisting
is unchanged. Four horizontal routing rows are a fixed policy, not an
optimality claim.
