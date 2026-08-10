API Reference
=============

``import miniflash as flash`` exposes every public name at the package
top level (``flash.parse``, ``flash.partition``, ...); the home modules
below hold the implementations and reference docstrings.

**Frontend** — circuit to Program IR:

.. toctree::
   :maxdepth: 1

   parse
   partition
   schedule
   floorplan
   placement1d
   placement2d
   channel
   synthesis
   orientation
   program
   factory

**Backend** — Program IR to geometry and glTF:

.. toctree::
   :maxdepth: 1

   lower
   gltf
