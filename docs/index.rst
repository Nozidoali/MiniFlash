MiniFlash
=========

**A mini and fast Clifford+T → lattice-surgery compiler.**

MiniFlash turns a Clifford+T circuit (OpenQASM 2.0) into a
lattice-surgery **tile program**: which tiles hold qubits, magic states and
routes, and which tiles merge at each step. It minimizes spacetime volume —
chip height × width × steps, including the steps spent moving qubits — and an
independent verifier checks every program before its volume is reported. The
result renders to a self-contained glTF scene you can open in any 3-D viewer.

.. grid:: 1 1 3 3
   :gutter: 3

   .. grid-item-card:: Quickstart
      :link: quickstart
      :link-type: doc

      Install and compile your first circuit into a verified tile
      program and a glTF scene.

   .. grid-item-card:: Benchmarks
      :link: benchmarks
      :link-type: doc

      Verified volume of the default solver on every shipped
      benchmark circuit.

   .. grid-item-card:: API reference
      :link: api/index
      :link-type: doc

      The pipeline objects — Problem, Mapping, Route, Program — and
      the solvers that connect them.

.. toctree::
   :hidden:
   :caption: Getting Started

   quickstart

.. toctree::
   :hidden:
   :caption: Results

   benchmarks

.. toctree::
   :hidden:
   :caption: Reference

   api/index
   driver
