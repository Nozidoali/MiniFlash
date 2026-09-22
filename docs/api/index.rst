API Reference
=============

The data flows through five objects, each in its own module::

   read_qasm / Problem.builder
             │
          Problem                 ordered pairs + dependency DAG
             │ map
          Mapping ← Layout        physical tiles ← virtual site order
             │ route
          list[list[Route]]       one list of disjoint routes per step
             │ compact / stage    shrink coordinates; insert walks
          Program                 sparse tile graph at every step
             │ verify / write_gltf
          Report, .gltf           checked volume; 3-D scene

Input
-----

.. automodule:: miniflash.parse
.. automodule:: miniflash.circuit

Geometry
--------

.. automodule:: miniflash.mapping
.. automodule:: miniflash.route

The Compiler
------------

.. automodule:: miniflash.compiler
.. automodule:: miniflash.solver.placement
.. automodule:: miniflash.solver.schedule
.. automodule:: miniflash.solver.routing
.. automodule:: miniflash.solver.spacing
.. automodule:: miniflash.solver.layout

Output
------

.. automodule:: miniflash.program
.. automodule:: miniflash.verify
.. automodule:: miniflash.factory
.. automodule:: miniflash.gltf
