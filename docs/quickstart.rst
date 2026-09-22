Quickstart
==========

Install
-------

.. code-block:: bash

   git clone https://github.com/Nozidoali/MiniFlash.git
   cd MiniFlash
   pip install -e '.[test]'
   python -m pytest -q tests

MiniFlash is pure Python on top of qiskit (QASM parsing) and NumPy
(spectral placement).

First Compile
-------------

.. code-block:: bash

   python main.py benchmarks/algorithms/ghz8.qasm -o ghz8.gltf

The driver prints one CSV row (grid, steps, walk steps, volume, timings) after
the program passes the verifier. Open ``ghz8.gltf`` in any glTF viewer, e.g.
the `three.js viewer <https://gltf-viewer.donmccurdy.com/>`_. Time runs
upward and every step spans two layers. Each qubit is a red vertical pipe. A CX
merge is split at its first turn, the ancilla: the half from the control's Z
side lies on the lower layer (blue), a blue vertical pipe climbs at the
ancilla, and the half into the target's X side lies on the upper layer (red).
T merges lie on the lower layer and end at a green magic state; grey cubes mark
occupied tiles.

Several circuits at once give a CSV table:

.. code-block:: bash

   python main.py benchmarks/toffoli/*.qasm --solver placement > toffoli.csv

As a Library
------------

.. code-block:: python

   import miniflash as flash

   problem = flash.read_qasm("benchmarks/toffoli/tof-3.qasm")
   program = flash.make("layout", seed=0).solve(problem)
   report = flash.verify(problem, program)
   print(report.steps, report.volume)
   program.save("tof3.json")
   flash.write_gltf(program, "tof3.gltf")

The Compiler Ladder
-------------------

One class, ``Compiler``, turns each component on in turn; ``make(name)`` builds
a preset and ``--solver`` selects it on the command line.

.. list-table::
   :header-rows: 1
   :widths: 18 82

   * - preset
     - adds
   * - ``vanilla``
     - sparse square, index placement, one pair per step
   * - ``dependency``
     - as many pairs per step as dependencies and free routes allow
   * - ``routing``
     - criticality-ordered routing that scores the lanes each route opens
   * - ``spacing``
     - partitions under a lane budget, compacted separately, with walks between
   * - ``placement``
     - spectral placement and annealing (moves and swaps) on the paired-lane array
   * - ``layout``
     - the site shape: predicted from the DAG (default), or ``--shapes N`` compares the N smallest

``compact``, ``staged`` and ``placed`` are development controls.
``--factory HxWxT`` sets the magic-state factory: H tiles deep, W along the chip
side, one state every T steps. Alone it puts one factory behind every magic tile;
with ``--factories F``, F shared factories feed the whole chip through delivery
rings, which ``-o`` draws. Without ``--factory`` magic supply is unlimited.

The Execution Model
-------------------

A CX is a pair ``(control, target)`` and a T/Tdg a pair ``(qubit, MAGIC)``;
dependencies follow program order on each qubit. Single-qubit Cliffords are
counted and treated as free. Every qubit faces the same way: its Z boundary is
above and below it and its X boundary left and right. A pair executes in one
step as a route of free tiles from the Z side of its Z party to the X side of
its X party, or to any side of a fresh magic tile. Between partitions qubits walk at most one tile per axis per step,
and a step with movement carries no merges.
