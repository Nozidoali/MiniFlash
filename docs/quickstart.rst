Quickstart
==========

Install
-------

.. code-block:: bash

   git clone https://github.com/Nozidoali/MiniFlash.git
   cd MiniFlash
   pip install -e .
   python scripts/download_lassynth.py

The last step fetches `LaSsynth <https://arxiv.org/abs/2404.18369>`_ —
the SAT cell synthesizer MiniFlash is built on — from its official
Zenodo artifact (sha256-pinned) into the repo root. Everything else
(qiskit, stim, z3, networkx, stimzx) comes with ``pip install``.

First Compile
-------------

.. code-block:: bash

   python main.py benchmarks/algorithms/ghz8.qasm -o ghz8.gltf

Open ``ghz8.gltf`` in any glTF viewer — e.g. the
`three.js viewer <https://gltf-viewer.donmccurdy.com/>`_ or the VS Code
glTF extension. Red/blue pipes are wire parity, grey cubes are
junctions, yellow slabs are Hadamards, green volume is magic-state
production.

``benchmarks/`` ships 175 circuits to try (GHZ, Bernstein–Vazirani,
Deutsch–Jozsa, random Clifford, graph states, Galois-field
multipliers, Toffoli, ...).

.. tip::

   Compilation starts cold and warms a per-cell disk cache as it goes
   (``.miniflash-cache/``). Pre-warm it from the published archive to
   skip the SAT solving on common cells:

   .. code-block:: bash

      python scripts/download_cache.py

The SAT Solver
--------------

Cell synthesis solves SAT instances. MiniFlash uses
`kissat <https://github.com/arminbiere/kissat>`_ when available — build
it and point ``MINIFLASH_KISSAT_DIR`` at the directory holding the
``kissat`` binary (or put ``kissat`` on ``PATH``) — and falls back to
z3 otherwise. z3 works but is markedly slower on cold cells.

CLI Reference
-------------

.. list-table::
   :header-rows: 1
   :widths: 26 60

   * - flag
     - meaning
   * - ``-o / --out``
     - write the glTF scene to this path (default: print stats only)
   * - ``--factory``
     - magic state factory: ``15-to-1`` | ``t-cultivation`` | ``external``
       or custom ``IxJxK`` dims (e.g. ``6x2x11``)
   * - ``--die WIDTH``
     - die width constraint (rows grow on demand)
   * - ``--factories``
     - number of factory units (die mode)
   * - ``--sat [SECONDS]``
     - SAT-solve uncached regions, per-region budget in seconds
       (default: cached cells + templates only)
   * - ``--side-ports``
     - swap through cell side faces (1-D only)
   * - ``--orientation``
     - lay deep cells down (1-D only)
   * - ``--cache-dir``
     - cell cache directory (default ``.miniflash-cache``)
   * - ``--dump-ir``
     - also write the Program IR as ``.program.json``

Partitioning is coarse-first: regions start at whole-circuit
granularity and split in place whenever a region exhausts its SAT
budget (``--sat`` mode; the default serves cached cells and templates
everything else).

Using MiniFlash as a Library
----------------------------

The pipeline runs to the **Program IR**, and the package's own backend
renders it:

.. code-block:: python

   import miniflash as flash

   circuit = flash.parse("circuit.qasm")
   pc = flash.partition(circuit)     # PartitionedCircuit: .regions / .events
   print(pc.to_text(color=True))     # region-tagged timeline; .save_png(...) for a figure

   floorplan, cells, channels = flash.synthesize(pc)
   program = flash.elaborate(floorplan, cells, events=pc.events, channels=channels)

   stats = program.stats()                       # check -> measure (no pipes materialized)
   flash.write_gltf(program, "layout.gltf")

   import json
   json.dump(program.stats(), open("stats.json", "w"), indent=2)

``program.stats()`` carries the volume metrics (``volume``,
``cube_envelope``), per-region Pauli frames, and —
for T circuits — injection readout policies and correction tables.
