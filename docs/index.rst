MiniFlash
=========

**A mini and fast Clifford+T → lattice-surgery compiler.**

MiniFlash turns a Clifford+T circuit (OpenQASM 2.0) into a
fault-tolerant lattice-surgery layout — the explicit 3-D spacetime
volume that a surface-code quantum computer would execute, with logical
qubits traced as pipes and every surgery, Hadamard and magic-state
injection placed. The result is a self-contained glTF scene you can
open in any 3-D viewer. Cell synthesis is
`LaSsynth <https://arxiv.org/abs/2404.18369>`_ (Tan, Niu, Gidney —
ISCA 2024).

.. grid:: 1 1 3 3
   :gutter: 3

   .. grid-item-card:: Quickstart
      :link: quickstart
      :link-type: doc

      Install, fetch LaSsynth, and compile your first circuit into a
      glTF scene in three commands.

   .. grid-item-card:: Benchmarks
      :link: benchmarks
      :link-type: doc

      Compiled volume against TopoLS and DasCot across 44 Clifford and
      graph-state circuits.

   .. grid-item-card:: API reference
      :link: api/index
      :link-type: doc

      The pipeline stages — parse to Program IR to glTF — as a small
      public API.

How It Works
------------

Routing is decided *before* synthesis. A floorplan fixes every port
column, order and color symbolically first; each cell is then
synthesized by SAT to match its pre-assigned ports. The wire never
adapts to the cell; the cell adapts to the wire — so routedness is
structural, and there is no routing retry loop to fail.

.. code-block:: text

   circuit.qasm
      │  parse ─ partition          PartitionedCircuit (regions + T events)
      ▼
   schedule ─ floorplan             layers, ports, lanes, channel moves — symbolic
      │
   synthesize (per cell)            LaSsynth SAT, canonical disk cache
      │
   elaborate  →  Program IR         macros / nets / channels
      │
   build_layout ─ write_gltf        check → lower → 3-D pipes → glTF
      ▼
   layout.gltf

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
