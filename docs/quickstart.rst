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
     - output ``.gltf`` path
   * - ``--cache-dir``
     - cell cache directory (default ``.miniflash-cache``)
   * - ``--budget``
     - per-region SAT seconds before the region is split (default 600)
   * - ``--max-gates``
     - floor of the per-region gate cap
   * - ``--factory``
     - magic state factory preset (``15-to-1`` | ``t-cultivation``)
   * - ``--factory-dims I J K``
     - custom factory footprint / production interval
   * - ``--die-dims WIDTH ROWS``
     - die constraint (hard width; ``ROWS 0`` = grow on demand)
   * - ``--factories``
     - number of factory units (die mode)
   * - ``--side-ports``
     - swap through cell side faces
   * - ``--dump-ir``
     - also write the Program IR as ``.program.json``

Partitioning is coarse-first: regions start at whole-circuit
granularity and split in place whenever a region exhausts its SAT
budget.

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

   layout = flash.build_layout(program)          # check -> lower
   flash.write_gltf(layout, "layout.gltf")

   import json
   json.dump(program.stats(), open("stats.json", "w"), indent=2)

``program.stats()`` carries the volume metrics (``volume``,
``occupied_volume``, ``cube_envelope``), per-region Pauli frames, and —
for T circuits — injection readout policies and correction tables.
