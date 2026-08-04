Driver and utilities
====================

``main.py`` is the repo-root driver + CLI: ``compile()`` runs the
package pipeline to a Program, ``main()`` emits glTF + stats through
``miniflash.gltf``. ``scripts/download_lassynth.py`` fetches LaSsynth
from its Zenodo artifact; ``scripts/download_cache.py`` pre-warms a cell
cache from the published archive.

main (driver + CLI)
-------------------

.. automodule:: main
   :members:

scripts.download_lassynth
-------------------------

.. automodule:: scripts.download_lassynth
   :members:

scripts.download_cache
----------------------

.. automodule:: scripts.download_cache
   :members:
