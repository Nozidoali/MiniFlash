Driver and utilities
====================

``main.py`` is the repo-root driver + CLI: ``compile()`` runs the
package pipeline to a Program, ``main()`` emits glTF through
``miniflash.gltf``. Two standalone scripts fetch external assets:
``scripts/download_lassynth.py`` (the LaSsynth synthesizer, from its
Zenodo artifact) and ``scripts/download_cache.py`` (the pre-warmed cell
cache, from the GitHub release).

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
